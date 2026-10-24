import gc
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager

import anyio
import anyio.to_thread
import httpx
import pytest
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
        with client, pytest.raises(PoolTimeout):
            _report(client)
    finally:
        client.app.state.pool.putconn(held)


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
