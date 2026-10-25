import gc
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace

import anyio
import anyio.to_thread
import httpx
import pytest
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus
from psycopg_pool import PoolTimeout

from verifair import db, notify
from verifair.app import create_app
from verifair.db import (
    AlreadyCreated,
    LastAdmin,
    add_comment,
    add_user,
    add_verdict,
    connect,
    create_token,
    init_schema,
    list_tokens,
    push_report,
    reopen_thread,
    resolve_thread,
    update_user,
)


@pytest.fixture
def url(new_database_url):
    url = new_database_url()
    c = connect(url)
    init_schema(c)
    c.close()
    return url


def _slow(monkeypatch, name, seconds=0.1):
    original = getattr(db, name)

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        time.sleep(seconds)
        return result

    monkeypatch.setattr(db, name, slow)


def _race(url, work, n=2):
    barrier = threading.Barrier(n)

    def task(i):
        c = connect(url)
        try:
            barrier.wait()
            return work(c, i)
        finally:
            c.close()

    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(task, range(n)))


def _push(c, **kw):
    return push_report(c, project="p", title="T", risk="low", body="b", **kw)


def test_concurrent_pushes_of_same_new_id_get_versions_1_and_2(url, monkeypatch):
    _slow(monkeypatch, "_lock_report")
    results = _race(url, lambda c, _: _push(c, id="r1"))
    assert sorted(results) == [("r1", 1), ("r1", 2)]


@pytest.mark.parametrize("existing", [True, False], ids=["existing-id", "same-new-id"])
@pytest.mark.parametrize("replacement_writer", [0, 1])
def test_concurrent_plan_inheritance_follows_previous_version(
    url, monkeypatch, existing, replacement_writer
):
    with closing(connect(url)) as conn:
        if existing:
            _push(conn, id="r1", plan="A")
        _slow(monkeypatch, "_lock_report")

        def work(c, i):
            replacing = i == replacement_writer
            return push_report(
                c,
                project="p",
                title="T",
                risk="low",
                id="r1",
                body="replacement" if replacing else "omitted",
                **({"plan": "B"} if replacing else {}),
            )

        results = _race(url, work)
        versions = db.list_versions(conn, "r1")
    assert sorted(results) == [("r1", n) for n in ([2, 3] if existing else [1, 2])]
    assert [v["version"] for v in versions] == ([1, 2, 3] if existing else [1, 2])
    previous = None
    for version in versions:
        if version["body"] == "omitted":
            assert version["plan"] == previous
        else:
            assert version["plan"] == ("B" if version["body"] == "replacement" else "A")
        previous = version["plan"]


def test_twenty_parallel_pushes_of_one_title_get_distinct_slugs(url):
    results = _race(url, lambda c, _: _push(c), n=20)
    assert sorted(results) == sorted([("t", 1)] + [(f"t-{n}", 1) for n in range(2, 21)])


def test_twenty_parallel_pushes_to_one_id_get_versions_1_to_20(url):
    results = _race(url, lambda c, _: _push(c, id="r1"), n=20)
    assert sorted(v for _, v in results) == list(range(1, 21))


def test_concurrent_demotion_of_last_two_admins_leaves_one(url, monkeypatch):
    c = connect(url)
    add_user(c, id="a", name="A", role="admin")
    add_user(c, id="b", name="B", role="admin")
    _slow(monkeypatch, "get_user")

    def demote(c, i):
        try:
            update_user(c, "ab"[i], role="member")
            return "demoted"
        except LastAdmin:
            return "refused"

    results = _race(url, demote)
    admins = c.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND active=1")
    admins_count = admins.fetchone()["n"]
    c.close()
    assert sorted(results) == ["demoted", "refused"]
    assert admins_count == 1


def test_double_submit_of_one_nonce_creates_one_token(url, monkeypatch):
    c = connect(url)
    add_user(c, id="m", name="M", role="member")
    _slow(monkeypatch, "_digest")

    def submit(c, _):
        try:
            create_token(c, owner_id="m", label="laptop", nonce="n1")
            return "created"
        except AlreadyCreated:
            return "AlreadyCreated"

    results = _race(url, submit)
    tokens = list_tokens(c, "m")
    c.close()
    assert sorted(results) == ["AlreadyCreated", "created"]
    assert len(tokens) == 1


# A holds the report lock for 0.3 s; without it B finishes at ~0.1 s.
# The 0.25 s bound leaves room for thread start skew after the barrier.
def _write_during_approval_took(url, write):
    b_took = []

    def work(c, i):
        started = time.monotonic()
        if i == 0:
            add_verdict(
                c,
                report_id="r1",
                version=1,
                reviewer_id="a",
                reviewer_name="A",
                verdict="approve",
                validate=lambda: time.sleep(0.3) or None,
            )
        else:
            time.sleep(0.1)
            write(c)
            b_took.append(time.monotonic() - started)

    _race(url, work)
    return b_took[0]


def test_blocking_comment_waits_for_an_approval_in_flight(url):
    c = connect(url)
    _push(c, id="r1")
    c.close()
    took = _write_during_approval_took(
        url,
        lambda c: add_comment(
            c, report_id="r1", version=1, author_id="b", author_name="B", body="no", blocking=True
        ),
    )
    assert took >= 0.25


def test_reopening_a_blocking_thread_waits_for_an_approval_in_flight(url):
    c = connect(url)
    _push(c, id="r1")
    root = add_comment(c, report_id="r1", version=1, author_id="b", author_name="B", body="no")
    resolve_thread(c, "r1", root["id"])
    c.close()
    took = _write_during_approval_took(url, lambda c: reopen_thread(c, "r1", root["id"]))
    assert took >= 0.25


def test_two_replicas_start_on_an_empty_database_at_once(shared_settings, monkeypatch):
    _slow(monkeypatch, "init_schema")
    barrier = threading.Barrier(2)
    apps, errors = [], []

    def start():
        barrier.wait()
        try:
            apps.append(create_app(shared_settings))
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=start) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for app in apps:
        app.state.pool.close()
    assert errors == []
    with closing(db.connect(shared_settings.database_url)) as c:
        assert [u["id"] for u in db.list_users(c)] == ["admin@example.com"]


def _report(client, id="r1"):
    payload = {"id": id, "project": "p", "title": "T", "risk": "low", "body": "b"}
    return client.post("/api/reports", json=payload, headers={"Authorization": "Bearer t"})


def test_wait_does_not_hold_a_connection_between_ticks(client):
    assert client.app.state.settings.db_pool_size == 1
    # one event loop for both threads: the pool semaphore binds to the first loop that waits
    with client:
        _report(client)
        waiting = threading.Thread(
            target=client.get,
            args=("/api/reports/r1/wait?timeout=2",),
            kwargs={"headers": {"Authorization": "Bearer t"}},
        )
        waiting.start()
        time.sleep(0.3)
        started = time.monotonic()
        second_push = _report(client)
        push_seconds = time.monotonic() - started
        waiting.join()
    assert second_push.status_code == 201
    assert push_seconds < 1.5


def test_wait_reuses_pooled_connections(client):
    _report(client)
    pool = client.app.state.pool
    before = pool.get_stats()
    response = client.get("/api/reports/r1/wait?timeout=1.2", headers={"Authorization": "Bearer t"})
    after = pool.get_stats()
    assert response.json()["event"] == "timeout"
    assert after.get("requests_num", 0) - before.get("requests_num", 0) >= 2
    assert after.get("connections_num", 0) == before.get("connections_num", 0)


def test_pool_connection_is_idle_when_a_request_releases_it(client, monkeypatch):
    pool = client.app.state.pool
    real = pool.connection
    released = []

    # pool.connection() commits on exit, so only the status just before that shows a leak
    @contextmanager
    def checked(*args, **kwargs):
        with real(*args, **kwargs) as conn:
            try:
                yield conn
            finally:
                released.append(conn.info.transaction_status)

    monkeypatch.setattr(pool, "connection", checked)
    _report(client)
    _report(client)
    client.cookies.set("vf_name", "Alice")
    stale = client.post("/r/r1/comments", data={"version": "1", "section": "A", "body": "late"})
    assert "A newer version" in stale.text
    response = client.get("/api/reports/r1", headers={"Authorization": "Bearer t"})
    assert response.status_code == 200
    assert len(released) >= 4
    assert set(released) == {TransactionStatus.IDLE}


def _set_worker_threads(client, n):
    async def set_tokens():
        anyio.to_thread.current_default_thread_limiter().total_tokens = n

    client.portal.call(set_tokens)


# More requests than worker threads on a pool of one: before the fix every worker thread
# sat in pool.getconn() while the request holding the connection waited for a thread.
def test_more_requests_than_worker_threads_all_succeed(client):
    client.app.state.pool.timeout = 2
    with client:
        _set_worker_threads(client, 3)
        with ThreadPoolExecutor(12) as pool:
            codes = list(pool.map(lambda i: _report(client, f"r{i}").status_code, range(12)))
    assert codes == [201] * 12


def test_pool_exhaustion_is_a_timeout_not_a_hang(client):
    client.app.state.pool.timeout = 0.5
    held = client.app.state.pool.getconn()
    try:
        with client:
            assert _report(client).status_code == 503
    finally:
        client.app.state.pool.putconn(held)


def test_waiting_for_a_slot_and_a_connection_takes_one_pool_timeout_in_total(client):
    pool, slots = client.app.state.pool, client.app.state.pool_slots
    pool.timeout = 1
    held = pool.getconn()
    try:
        with client:
            client.portal.call(slots.acquire)
            threading.Timer(0.6, client.portal.call, (slots.release,)).start()
            started = time.monotonic()
            status = _report(client).status_code
            took = time.monotonic() - started
    finally:
        pool.putconn(held)
    assert status == 503
    assert 0.9 < took < 1.3


def test_healthz_needs_no_worker_thread(client):
    holder = object()

    async def hog():
        await anyio.to_thread.current_default_thread_limiter().acquire_on_behalf_of(holder)

    async def free():
        anyio.to_thread.current_default_thread_limiter().release_on_behalf_of(holder)

    answers = []
    with client:
        _set_worker_threads(client, 1)
        client.portal.call(hog)
        probe = threading.Thread(target=lambda: answers.append(client.get("/healthz").json()))
        probe.start()
        probe.join(2)
        answered = list(answers)
        client.portal.call(free)
        probe.join()
    assert answered == [{"ok": True}]


def test_connection_is_back_in_the_pool_before_background_tasks_run(client, monkeypatch):
    borrowed = []

    def send(settings, text):
        try:
            with client.app.state.pool.connection(timeout=0.5):
                borrowed.append(True)
        except PoolTimeout:
            borrowed.append(False)

    monkeypatch.setattr(notify, "send", send)
    assert _report(client).status_code == 201
    assert borrowed == [True]


# A dropped request used to hand its connection back only when the garbage collector found it
def test_a_cancelled_request_gives_its_connection_back(settings, make_app, monkeypatch):
    app = make_app(settings)
    app.state.pool.timeout = 1
    auth = {"Authorization": "Bearer t"}
    payload = {"id": "r1", "project": "p", "title": "T", "risk": "low", "body": "b"}
    _slow(monkeypatch, "get_signature", 0.5)

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            pushed = await client.post("/api/reports", json=payload, headers=auth)
            assert pushed.status_code == 201
            with anyio.move_on_after(0.1) as scope:
                await client.get("/api/reports/r1", headers=auth)
            assert scope.cancelled_caught
            with anyio.fail_after(3):
                return (await client.post("/api/reports", json=payload, headers=auth)).status_code

    gc.disable()
    try:
        assert anyio.run(main) == 201
    finally:
        gc.enable()
    assert app.state.pool_slots._value == 1


def test_a_request_after_the_database_dropped_the_pooled_connection_succeeds(client, admin_conn):
    _report(client)
    with client.app.state.pool.connection() as c:
        pid = c.info.backend_pid
    admin_conn.execute("SELECT pg_terminate_backend(%s, 2000)", (pid,))
    response = client.get("/api/reports/r1", headers={"Authorization": "Bearer t"})
    assert response.status_code == 200


# after a database restart every idle connection is dead; trying them one by one
# with the pool's backoff (0, 1, 2, 4 s) made the first request hang
def test_first_request_after_all_pooled_connections_died_is_fast(settings, make_app, admin_conn):
    client = TestClient(make_app(replace(settings, db_pool_size=3)))
    pool = client.app.state.pool
    assert _report(client).status_code == 201
    held = [pool.getconn() for _ in range(3)]
    pids = [c.info.backend_pid for c in held]
    for c in held:
        pool.putconn(c)
    admin_conn.execute("SELECT pg_terminate_backend(pid, 2000) FROM unnest(%s::int[]) pid", (pids,))
    started = time.monotonic()
    response = client.get("/api/reports/r1", headers={"Authorization": "Bearer t"})
    assert response.status_code == 200
    assert time.monotonic() - started < 0.5


def test_a_verdict_in_flight_keeps_the_review_request(url):
    c = connect(url)
    _push(c, id="r1")
    db.add_review_request(c, "r1", "a")
    removed = []

    def work(c, i):
        if i == 0:
            add_verdict(
                c,
                report_id="r1",
                version=1,
                reviewer_id="a",
                reviewer_name="A",
                verdict="approve",
                validate=lambda: time.sleep(0.3) or None,
            )
        else:
            time.sleep(0.1)
            removed.append(db.remove_review_request(c, "r1", "a"))

    _race(url, work)
    requested = db.requested_reviewers(c, "r1")
    c.close()
    assert removed == ["kept"] and requested == ["a"]


def _plan_seed(conn, *, implementation=False):
    db.push_report(
        conn,
        id="plan",
        project="p",
        title="Plan",
        risk="low",
        body=None,
        body_supplied=False,
        plan="A",
        workflow="plan_first",
    )
    db.sign_version(
        conn,
        report_id="plan",
        target="plan",
        version=1,
        expected_version=1,
        signer_id="owner",
        signer_name="Owner",
    )
    db.add_verdict(
        conn,
        report_id="plan",
        target="plan",
        version=1,
        reviewer_id="reviewer",
        reviewer_name="Reviewer",
        verdict="approve",
    )
    if implementation:
        db.push_report(
            conn, id="plan", project="p", title="Plan", risk="low", body="body", expected_version=1
        )


def _ordered_plan_race(url, monkeypatch, first, second):
    held, attempted = threading.Event(), threading.Event()
    original = db._lock_report
    second_pid = []

    def lock(conn, report_id):
        if conn.info.backend_pid in second_pid:
            attempted.set()
        return original(conn, report_id)

    monkeypatch.setattr(db, "_lock_report", lock)

    def winner():
        with closing(connect(url)) as conn, conn.transaction():
            result = first(conn)
            held.set()
            assert attempted.wait(3)
            return result

    def follower():
        with closing(connect(url)) as conn:
            second_pid.append(conn.info.backend_pid)
            assert held.wait(3)
            return second(conn)

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(winner), pool.submit(follower)
        return a.result(timeout=5), b.result(timeout=5)


@pytest.mark.parametrize("action", ["attach", "body", "sign", "approve", "request_changes"])
@pytest.mark.parametrize("close", ["edit", "reopen", "new_blocker", "make_blocking"])
@pytest.mark.parametrize("closing_first", [True, False])
def test_plan_gate_races_all_implementation_writers(url, monkeypatch, action, close, closing_first):
    snapshot = 1 if action == "attach" else 2
    body_action = action in ("attach", "body")
    with closing(connect(url)) as conn:
        _plan_seed(conn, implementation=action != "attach")
        if action in ("approve", "request_changes"):
            db.sign_version(
                conn, report_id="plan", version=snapshot, signer_id="owner", signer_name="O"
            )
        root = db.add_comment(
            conn,
            report_id="plan",
            target="plan",
            version=1,
            author_id="r",
            author_name="R",
            body="concern",
            blocking=close != "make_blocking",
        )
        if close != "make_blocking":
            db.resolve_thread(conn, "plan", root["id"])

    def close_plan(conn):
        if close == "edit":
            expected = snapshot + int(body_action and not closing_first)
            return db.push_report(
                conn,
                id="plan",
                project="p",
                title="Plan",
                risk="low",
                body=None,
                body_supplied=False,
                plan="B",
                expected_version=expected,
            )
        if close == "reopen":
            return db.reopen_thread(conn, "plan", root["id"])
        if close == "make_blocking":
            return db.set_blocking(conn, "plan", root["id"], True)
        return db.add_comment(
            conn,
            report_id="plan",
            target="plan",
            version=1,
            author_id="r",
            author_name="R",
            body="new concern",
        )

    def implementation(conn):
        try:
            if body_action:
                result = db.push_report(
                    conn,
                    id="plan",
                    project="p",
                    title="Attached",
                    risk="high",
                    body="new body",
                    expected_version=snapshot,
                    reviewers=["new"],
                )
            elif action == "sign":
                result = db.sign_version(
                    conn, report_id="plan", version=snapshot, signer_id="owner", signer_name="O"
                )
            else:
                result = db.add_verdict(
                    conn,
                    report_id="plan",
                    version=snapshot,
                    expected_version=snapshot,
                    reviewer_id="r",
                    reviewer_name="R",
                    verdict=action,
                )
            return "saved", result
        except (db.StaleVersion, db.PlanNotApproved) as exc:
            return "refused", exc

    first, second = (close_plan, implementation) if closing_first else (implementation, close_plan)
    results = _ordered_plan_race(url, monkeypatch, first, second)
    assert results[1 if closing_first else 0][0] == ("refused" if closing_first else "saved")
    with closing(connect(url)) as conn:
        state = db.plan_review_state(conn, "plan")
        assert state["status"] == ("unsigned" if close == "edit" else "changes_requested")
        assert db.get_report(conn, "plan")["title"] == (
            "Plan" if closing_first or close == "edit" else "Attached" if body_action else "Plan"
        )
        if body_action:
            assert db.requested_reviewers(conn, "plan") == ([] if closing_first else ["new"])
            assert db.get_version(conn, "plan", snapshot)["body"] == (
                None if action == "attach" else "body"
            )
            if not closing_first:
                assert db.get_version(conn, "plan", snapshot + 1)["body"] == "new body"
        if action == "sign":
            assert (db.get_signature(conn, "plan", 2) is None) == closing_first
        if action in ("approve", "request_changes"):
            assert len(db.list_verdicts(conn, "plan")) == (0 if closing_first else 1)


def test_plan_concurrent_updates_one_guard_has_one_winner(url):
    with closing(connect(url)) as conn:
        _plan_seed(conn)

    def update(conn, i):
        try:
            return db.push_report(
                conn,
                id="plan",
                project="p",
                title="Plan",
                risk="low",
                body=None,
                body_supplied=False,
                plan=f"B{i}",
                expected_version=1,
            )
        except db.StaleVersion as exc:
            return exc.latest

    results = _race(url, update)
    assert sorted(results, key=str) == [("plan", 2), 2]


@pytest.mark.parametrize("workflow", ["artifact", "plan_first"])
def test_plan_same_id_creation_rechecks_winning_workflow_and_guard(url, monkeypatch, workflow):
    def initial(conn, selected):
        return db.push_report(
            conn,
            id="same",
            project="p",
            title="T",
            risk="low",
            workflow=selected,
            plan="A",
            body="body" if selected == "artifact" else None,
            body_supplied=selected == "artifact",
        )

    other = "artifact" if workflow == "plan_first" else "plan_first"

    def competing(conn):
        with pytest.raises(db.WorkflowMismatch):
            initial(conn, other)
        if workflow == "plan_first":
            with pytest.raises(ValueError, match="expected_version"):
                initial(conn, workflow)

    _ordered_plan_race(url, monkeypatch, lambda c: initial(c, workflow), competing)
    with closing(connect(url)) as conn:
        assert db.latest_version(conn, "same") == 1
        assert db.get_report(conn, "same")["workflow"] == workflow


def test_plan_stale_approval_after_edit_cannot_approve_new_text(url, monkeypatch):
    with closing(connect(url)) as conn:
        _plan_seed(conn)

    def edit(conn):
        db.push_report(
            conn,
            id="plan",
            project="p",
            title="Plan",
            risk="low",
            body=None,
            body_supplied=False,
            plan="B",
            expected_version=1,
        )

    def approve(conn):
        with pytest.raises(db.StaleRevision) as exc:
            db.add_verdict(
                conn,
                report_id="plan",
                target="plan",
                version=1,
                reviewer_id="r",
                reviewer_name="R",
                verdict="approve",
            )
        assert exc.value.current_revision == 2

    _ordered_plan_race(url, monkeypatch, edit, approve)
    with closing(connect(url)) as conn:
        assert db.plan_review_state(conn, "plan")["status"] == "unsigned"
        assert len(db.list_verdicts(conn, "plan", target="plan")) == 1


@pytest.mark.parametrize("push_first", [True, False])
def test_plan_legacy_comment_validation_uses_locked_current_markdown(url, monkeypatch, push_first):
    with closing(connect(url)) as conn:
        _push(conn, id="r1")
    checked = []

    def push(conn):
        return db.push_report(conn, id="r1", project="p", title="T", risk="low", body="new body")

    def comment(conn):
        return db.add_comment(
            conn,
            report_id="r1",
            author_id="r",
            author_name="R",
            body="comment",
            validate=lambda markdown, revision: checked.append((markdown, revision)),
        )

    first, second = (push, comment) if push_first else (comment, push)
    results = _ordered_plan_race(url, monkeypatch, first, second)
    saved = results[1 if push_first else 0]
    assert checked == [("new body", 2) if push_first else ("b", 1)]
    assert saved["version"] == checked[0][1]
