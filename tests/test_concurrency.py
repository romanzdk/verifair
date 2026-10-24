import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from verifair import db
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


def test_blocking_comment_waits_for_an_approval_in_flight(url):
    c = connect(url)
    _push(c, id="r1")
    c.close()
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
            add_comment(
                c,
                report_id="r1",
                version=1,
                author_id="b",
                author_name="B",
                body="no",
                blocking=True,
            )
            b_took.append(time.monotonic() - started)

    _race(url, work)
    assert b_took[0] >= 0.3
