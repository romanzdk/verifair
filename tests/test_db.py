import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from verifair import db
from verifair.db import (
    ProjectMismatch,
    add_comment,
    connect,
    get_conn,
    get_report,
    init_schema,
    list_comments,
    list_projects,
    list_reports,
    list_versions,
    push_report,
    resolve_thread,
)


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_schema(c)
    yield c
    c.close()


def test_push_new_report_slugs_title(conn):
    result = push_report(
        conn, project="p", title="Prorate mid-cycle plan changes!", risk="high", body="# x"
    )
    assert result == ("prorate-mid-cycle-plan-changes", 1)


def test_push_same_title_twice_gets_suffix(conn):
    push_report(conn, project="p", title="Same", risk="low", body="a")
    assert push_report(conn, project="p", title="Same", risk="low", body="b")[0] == "same-2"


def test_push_existing_id_adds_version_and_overwrites_meta(conn):
    push_report(conn, project="p", title="T", risk="low", body="v1", id="r1")
    assert push_report(conn, project="p", title="T2", risk="high", body="v2", id="r1") == ("r1", 2)
    r = get_report(conn, "r1")
    assert (r["title"], r["risk"]) == ("T2", "high")
    assert [v["version"] for v in list_versions(conn, "r1")] == [1, 2]


def test_push_existing_id_other_project_refused(conn):
    push_report(conn, project="p", title="T", risk="low", body="v1", id="r1")
    with pytest.raises(ProjectMismatch):
        push_report(conn, project="q", title="T", risk="low", body="v2", id="r1")
    assert not conn.in_transaction


def test_reply_inherits_anchor_and_resolve_root_only(conn):
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    root = add_comment(
        conn,
        report_id="r1",
        version=1,
        author_id="u1",
        author_name="Alice",
        body="?",
        section="Intent",
        quote="full new price",
    )
    reply = add_comment(
        conn,
        report_id="r1",
        version=1,
        author_id="agent",
        author_name="agent",
        body="!",
        parent_id=root["id"],
    )
    assert (reply["section"], reply["quote"]) == ("Intent", "full new price")
    assert len(list_comments(conn, "r1", open_only=True)) == 2
    assert resolve_thread(conn, "r1", reply["id"]) is False
    assert resolve_thread(conn, "r1", root["id"]) is True
    assert list_comments(conn, "r1", open_only=True) == []
    assert len(list_comments(conn, "r1")) == 2


def test_list_reports_newest_first_with_latest_version(conn):
    push_report(conn, project="p", title="First", risk="low", body="a", id="r1")
    push_report(conn, project="p", title="Second", risk="low", body="b", id="r2")
    push_report(conn, project="other", title="Elsewhere", risk="low", body="c", id="r3")
    conn.execute("UPDATE reports SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = 'r1'")
    conn.execute("UPDATE reports SET created_at = '2026-01-02T00:00:00+00:00' WHERE id = 'r2'")

    rows = list_reports(conn, "p")
    assert [r["id"] for r in rows] == ["r2", "r1"]
    assert [r["latest_version"] for r in rows] == [1, 1]
    assert [r["open_threads"] for r in rows] == [0, 0]

    push_report(conn, project="p", title="First", risk="low", body="a2", id="r1")
    add_comment(conn, report_id="r1", version=2, author_id="u1", author_name="Alice", body="?")
    by_id = {r["id"]: r for r in list_reports(conn, "p")}
    assert by_id["r1"]["latest_version"] == 2
    assert by_id["r1"]["open_threads"] == 1
    assert by_id["r2"]["open_threads"] == 0


def test_list_projects_distinct_sorted(conn):
    for project in ("zeta", "alpha", "zeta"):
        push_report(conn, project=project, title="T", risk="low", body="b")
    assert list_projects(conn) == ["alpha", "zeta"]


def test_get_conn_route_write_is_visible_to_other_connection(settings):
    app = FastAPI()
    app.state.settings = settings
    init = connect(settings.db_path)
    init_schema(init)
    init.close()

    @app.post("/push")
    def push(conn: Annotated[sqlite3.Connection, Depends(get_conn)]):
        return push_report(conn, project="p", title="T", risk="low", body="b")

    assert TestClient(app).post("/push").json() == ["t", 1]
    other = connect(settings.db_path)
    try:
        assert get_report(other, "t")["project"] == "p"
    finally:
        other.close()


def test_concurrent_pushes_of_same_id_get_versions_1_and_2(tmp_path, monkeypatch):
    path = str(tmp_path / "race.db")
    init = connect(path)
    init_schema(init)
    init.close()
    original = db.get_report

    def slow_get_report(conn, id):
        row = original(conn, id)
        time.sleep(0.1)
        return row

    monkeypatch.setattr(db, "get_report", slow_get_report)

    def push(_):
        c = connect(path)
        try:
            return push_report(c, project="p", title="T", risk="low", body="b", id="r1")
        finally:
            c.close()

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(push, range(2)))
    assert sorted(results) == [("r1", 1), ("r1", 2)]
