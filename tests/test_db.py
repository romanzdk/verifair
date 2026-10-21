import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from verifair import db
from verifair.db import (
    AlreadySigned,
    LastAdmin,
    ProjectMismatch,
    StaleVersion,
    add_comment,
    add_user,
    add_verdict,
    authenticate_token,
    bootstrap_admin,
    connect,
    create_token,
    get_report,
    get_signature,
    get_user,
    get_version,
    init_schema,
    list_comments,
    list_projects,
    list_reports,
    list_tokens,
    list_users,
    list_versions,
    push_report,
    reopen_thread,
    resolve_thread,
    revoke_token,
    set_blocking,
    sign_version,
    update_user,
)
from verifair.deps import get_conn


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
        prefix="the ",
        suffix=" now",
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
    assert (reply["prefix"], reply["suffix"]) == ("the ", " now")
    assert len(list_comments(conn, "r1", open_only=True)) == 2
    assert resolve_thread(conn, "r1", reply["id"]) == root["id"]
    assert list_comments(conn, "r1", open_only=True) == []
    assert len(list_comments(conn, "r1")) == 2
    assert resolve_thread(conn, "r1", 999) is None


def _resolved_at(conn, id):
    return conn.execute("SELECT resolved_at FROM comments WHERE id=?", (id,)).fetchone()[0]


def test_resolve_twice_keeps_first_time_and_reply_reopens(conn):
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    kw = {"report_id": "r1", "version": 1, "author_id": "u1", "author_name": "Alice"}
    root = add_comment(conn, body="?", **kw)
    resolve_thread(conn, "r1", root["id"])
    conn.execute("UPDATE comments SET resolved_at='2000-01-01' WHERE id=?", (root["id"],))
    assert resolve_thread(conn, "r1", root["id"]) == root["id"]
    assert _resolved_at(conn, root["id"]) == "2000-01-01"
    add_comment(conn, body="still wrong", parent_id=root["id"], **kw)
    assert _resolved_at(conn, root["id"]) is None


def test_list_comments_orders_by_id(conn):
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    kw = {"report_id": "r1", "version": 1, "author_id": "u1", "author_name": "Alice"}
    root = add_comment(conn, body="?", **kw)
    reply = add_comment(conn, body="!", parent_id=root["id"], **kw)
    conn.execute("UPDATE comments SET created_at='2000-01-01' WHERE id=?", (reply["id"],))
    assert [c["id"] for c in list_comments(conn, "r1")] == [root["id"], reply["id"]]


def test_writes_check_expected_version_atomically(conn):
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    push_report(conn, project="p", title="T", risk="low", body="b2", id="r1")
    kw = {"report_id": "r1", "version": 1, "author_id": "u1", "author_name": "Alice"}
    with pytest.raises(StaleVersion) as exc:
        add_comment(conn, body="?", expected_version=1, **kw)
    assert exc.value.latest == 2
    with pytest.raises(StaleVersion):
        add_verdict(
            conn,
            report_id="r1",
            version=1,
            reviewer_id="u1",
            reviewer_name="Alice",
            verdict="approve",
            expected_version=1,
        )
    assert list_comments(conn, "r1") == []
    assert conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0
    assert not conn.in_transaction


def test_schema_checks_and_indexes(conn):
    with pytest.raises(sqlite3.IntegrityError):
        push_report(conn, project="p", title="T", risk="extreme", body="b")
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    with pytest.raises(sqlite3.IntegrityError):
        add_verdict(
            conn, report_id="r1", version=1, reviewer_id="u", reviewer_name="U", verdict="maybe"
        )
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"comments_report_parent", "verdicts_report", "reports_project"} <= indexes


def test_db_module_does_not_depend_on_fastapi():
    assert "fastapi" not in Path(db.__file__).read_text()


def test_writes_log_one_info_line(conn, caplog):
    caplog.set_level("INFO", logger="verifair")
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    add_comment(conn, report_id="r1", version=1, author_id="u1", author_name="Alice", body="?")
    add_verdict(
        conn, report_id="r1", version=1, reviewer_id="u1", reviewer_name="Alice", verdict="approve"
    )
    with pytest.raises(ProjectMismatch):
        push_report(conn, project="q", title="T", risk="low", body="b", id="r1")
    messages = [r.getMessage() for r in caplog.records if r.name == "verifair"]
    assert len(messages) == 4
    assert messages[0].startswith("report pushed") and "r1" in messages[0]
    assert messages[1].startswith("comment added") and messages[2].startswith("verdict added")
    assert messages[3].startswith("push rejected")


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


def test_reopen_thread_by_root_or_reply(conn):
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    kw = {"report_id": "r1", "version": 1, "author_id": "u1", "author_name": "Alice"}
    root = add_comment(conn, body="?", **kw)
    reply = add_comment(conn, body="!", parent_id=root["id"], **kw)
    resolve_thread(conn, "r1", root["id"])
    assert reopen_thread(conn, "r1", reply["id"]) is True
    assert _resolved_at(conn, root["id"]) is None
    assert reopen_thread(conn, "r1", 999) is False
    assert reopen_thread(conn, "other", root["id"]) is False


def test_info_logs_do_not_print_full_user_ids(conn, caplog):
    caplog.set_level("INFO", logger="verifair")
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    kw = {"report_id": "r1", "version": 1, "author_name": "Alice", "body": "?"}
    add_comment(conn, author_id="SeCrEtId12", **kw)
    add_verdict(
        conn,
        report_id="r1",
        version=1,
        reviewer_id="ReViEwEr99",
        reviewer_name="B",
        verdict="approve",
    )
    assert "SeCrEtId12" not in caplog.text and "ReViEwEr99" not in caplog.text
    assert "author_id=SeCr…" in caplog.text and "reviewer_id=ReVi…" in caplog.text


V1_SCHEMA = """
CREATE TABLE reports (
    id TEXT PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL,
    risk TEXT NOT NULL CHECK (risk IN ('low', 'medium', 'high')), ticket_url TEXT,
    links TEXT NOT NULL DEFAULT '[]', author TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE versions (
    report_id TEXT NOT NULL REFERENCES reports(id), version INTEGER NOT NULL,
    body TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (report_id, version)
);
CREATE TABLE comments (
    id INTEGER PRIMARY KEY, report_id TEXT NOT NULL REFERENCES reports(id),
    version INTEGER NOT NULL, parent_id INTEGER REFERENCES comments(id), section TEXT,
    quote TEXT, author_id TEXT NOT NULL, author_name TEXT NOT NULL, body TEXT NOT NULL,
    created_at TEXT NOT NULL, resolved_at TEXT
);
CREATE TABLE verdicts (
    id INTEGER PRIMARY KEY, report_id TEXT NOT NULL REFERENCES reports(id),
    version INTEGER NOT NULL, reviewer_id TEXT NOT NULL, reviewer_name TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('approve', 'request_changes')),
    summary TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
"""


def _columns(c, table):
    return {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}


def test_init_schema_migrates_v1_database(tmp_path):
    c = connect(str(tmp_path / "old.db"))
    c.executescript(V1_SCHEMA)
    c.execute("INSERT INTO reports VALUES ('r0', 'p', 'T', 'low', NULL, '[]', 'a', 'now', 'now')")
    c.execute("INSERT INTO versions VALUES ('r0', 1, 'b', 'now')")
    c.execute(
        "INSERT INTO comments (report_id, version, author_id, author_name, body, created_at)"
        " VALUES ('r0', 1, 'u', 'U', 'hi', 'now')"
    )
    init_schema(c)
    init_schema(c)
    assert {"agent", "model", "session_url", "author_id"} <= _columns(c, "reports")
    assert "commit" in _columns(c, "versions")
    assert {"blocking", "resolved_by"} <= _columns(c, "comments")
    assert c.execute("SELECT blocking FROM comments").fetchone()[0] == 1
    push_report(
        c, project="p", title="T", risk="low", body="b2", id="r0", agent="x", commit="abc1234"
    )
    assert get_version(c, "r0", 2)["commit"] == "abc1234"
    assert get_version(c, "r0", 1)["commit"] is None
    assert get_report(c, "r0")["agent"] == "x"
    c.close()


def test_push_stores_provenance_and_overwrites_on_new_version(conn):
    push_report(
        conn,
        project="p",
        title="T",
        risk="low",
        body="v1",
        id="r1",
        agent="a1",
        model="m1",
        session_url="http://s/1",
        commit="c1",
    )
    push_report(
        conn,
        project="p",
        title="T",
        risk="low",
        body="v2",
        id="r1",
        agent="a2",
        model="m2",
        session_url="http://s/2",
        commit="c2",
    )
    r = get_report(conn, "r1")
    assert (r["agent"], r["model"], r["session_url"]) == ("a2", "m2", "http://s/2")
    assert [v["commit"] for v in list_versions(conn, "r1")] == ["c1", "c2"]


def test_sign_version_stores_and_resigning_is_refused(conn):
    push_report(conn, project="p", title="T", risk="low", body="v1", id="r1")
    assert get_signature(conn, "r1", 1) is None
    sig = sign_version(conn, report_id="r1", version=1, signer_id="u1", signer_name="Ann")
    assert (sig["signer_id"], sig["signer_name"]) == ("u1", "Ann")
    with pytest.raises(AlreadySigned):
        sign_version(conn, report_id="r1", version=1, signer_id="u2", signer_name="Bob")
    assert get_signature(conn, "r1", 1)["signer_id"] == "u1"


def test_sign_version_refuses_stale_version(conn):
    push_report(conn, project="p", title="T", risk="low", body="v1", id="r1")
    push_report(conn, project="p", title="T", risk="low", body="v2", id="r1")
    with pytest.raises(StaleVersion) as exc:
        sign_version(conn, report_id="r1", version=1, signer_id="u1", signer_name="Ann")
    assert exc.value.latest == 2
    assert get_signature(conn, "r1", 1) is None


def test_comment_blocking_defaults_true_and_replies_copy_root(conn):
    push_report(conn, project="p", title="T", risk="low", body="v1", id="r1")
    kw = {"report_id": "r1", "version": 1, "author_id": "u", "author_name": "U"}
    blocking = add_comment(conn, body="a", **kw)
    soft = add_comment(conn, body="b", blocking=False, **kw)
    reply = add_comment(conn, body="c", parent_id=soft["id"], **kw)
    assert (blocking["blocking"], soft["blocking"], reply["blocking"]) == (1, 0, 0)


def test_set_blocking_root_only(conn):
    push_report(conn, project="p", title="T", risk="low", body="v1", id="r1")
    kw = {"report_id": "r1", "version": 1, "author_id": "u", "author_name": "U"}
    root = add_comment(conn, body="a", **kw)
    reply = add_comment(conn, body="b", parent_id=root["id"], **kw)
    assert set_blocking(conn, "r1", reply["id"], False) is False
    assert set_blocking(conn, "r1", 999, False) is False
    assert set_blocking(conn, "r1", root["id"], False) is True
    assert [c["blocking"] for c in list_comments(conn, "r1")] == [0, 1]


def test_legacy_signer_verdict_is_history_without_effective_self_approval(settings):
    from verifair.app import create_app

    with connect(settings.db_path) as old:
        old.executescript(V1_SCHEMA)
        old.execute(
            "INSERT INTO reports VALUES"
            " ('r0', 'p', 'Legacy report', 'low', NULL, '[]', 'Alice',"
            " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
        )
        old.execute(
            "INSERT INTO versions VALUES"
            " ('r0', 1, '## Verification\nLegacy evidence', '2026-01-01T00:00:00+00:00')"
        )
        old.execute(
            "INSERT INTO verdicts VALUES"
            " (1, 'r0', 1, 'alice000', 'Alice', 'approve', 'Old approval',"
            " '2026-01-01T00:00:00+00:00')"
        )
    old.close()
    upgraded = TestClient(create_app(settings), headers={"Sec-Fetch-Site": "same-origin"})
    headers = {"Authorization": "Bearer t"}
    assert upgraded.get("/api/reports/r0", headers=headers).json()["status"] == "unsigned"
    upgraded.cookies.set("vf_id", "alice000")
    upgraded.cookies.set("vf_name", "Alice")
    signed = upgraded.post("/r/r0/sign", data={"version": 1})
    assert signed.status_code == 200
    report = upgraded.get("/api/reports/r0", headers=headers).json()
    assert report["status"] == "awaiting_review"
    assert report["signed_by"] == "Alice"
    assert len(report["verdicts"]) == 1
    assert report["verdicts"][0]["summary"] == "Old approval"
    assert 'class="badge b-awaiting_review"' in upgraded.get("/r/r0").text
    assert "Awaiting review · 1" in upgraded.get("/p/p").text
    assert 'aria-label="Awaiting review: 1 change"' in upgraded.get("/").text
    upgraded.cookies.set("vf_id", "bob00000")
    upgraded.cookies.set("vf_name", "Bob")
    approved = upgraded.post("/r/r0/verdict", data={"version": 1, "verdict": "approve"})
    assert "You approved v1" in approved.text
    report = upgraded.get("/api/reports/r0", headers=headers).json()
    assert report["status"] == "approved" and len(report["verdicts"]) == 2


def _resolved_by(conn, id):
    return conn.execute("SELECT resolved_by FROM comments WHERE id=?", (id,)).fetchone()[0]


def test_resolved_by_is_kept_on_first_resolve_and_cleared_on_reopen(conn):
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    kw = {"report_id": "r1", "version": 1, "author_id": "u1", "author_name": "Alice"}
    root = add_comment(conn, body="?", **kw)
    reply = add_comment(conn, body="!", parent_id=root["id"], **kw)
    resolve_thread(conn, "r1", reply["id"], by="u2")
    assert _resolved_by(conn, root["id"]) == "u2"
    resolve_thread(conn, "r1", root["id"], by="u3")
    assert _resolved_by(conn, root["id"]) == "u2"
    reopen_thread(conn, "r1", root["id"])
    assert _resolved_by(conn, root["id"]) is None
    resolve_thread(conn, "r1", root["id"], by="u2")
    add_comment(conn, body="not yet", parent_id=root["id"], **kw)
    assert _resolved_by(conn, root["id"]) is None
    resolve_thread(conn, "r1", root["id"])
    assert _resolved_by(conn, root["id"]) is None
    assert _resolved_at(conn, root["id"]) is not None


def test_add_and_get_user(conn):
    user = add_user(conn, id="ann@example.com", name="Ann", role="member")
    assert (user["id"], user["name"], user["role"], user["active"]) == (
        "ann@example.com",
        "Ann",
        "member",
        1,
    )
    assert user["created_at"] == user["updated_at"]
    assert get_user(conn, "ann@example.com")["name"] == "Ann"
    assert get_user(conn, "nobody@example.com") is None


def test_add_user_refuses_duplicate(conn):
    add_user(conn, id="ann@example.com", name="Ann", role="member")
    with pytest.raises(ValueError):
        add_user(conn, id="ann@example.com", name="Other", role="admin")
    assert get_user(conn, "ann@example.com")["name"] == "Ann"
    assert not conn.in_transaction


def test_bad_role_is_refused(conn):
    with pytest.raises(ValueError):
        add_user(conn, id="bob@example.com", name="Bob", role="owner")
    assert get_user(conn, "bob@example.com") is None
    add_user(conn, id="bob@example.com", name="Bob", role="member")
    with pytest.raises(ValueError):
        update_user(conn, "bob@example.com", role="owner")
    assert get_user(conn, "bob@example.com")["role"] == "member"


def test_list_users_admins_first_then_by_name(conn):
    add_user(conn, id="z", name="Zed", role="member")
    add_user(conn, id="b", name="Bea", role="admin")
    add_user(conn, id="a", name="Al", role="member")
    add_user(conn, id="y", name="Yan", role="admin")
    assert [u["id"] for u in list_users(conn)] == ["b", "y", "a", "z"]


def test_update_user_changes_role_and_active(conn):
    add_user(conn, id="boss", name="Boss", role="admin")
    add_user(conn, id="m", name="M", role="member")
    conn.execute("UPDATE users SET updated_at='2000-01-01' WHERE id='m'")
    promoted = update_user(conn, "m", role="admin")
    assert (promoted["role"], promoted["active"]) == ("admin", 1)
    assert promoted["updated_at"] != "2000-01-01"
    off = update_user(conn, "m", active=False)
    assert (off["role"], off["active"]) == ("admin", 0)
    assert update_user(conn, "m", active=True)["active"] == 1
    with pytest.raises(ValueError):
        update_user(conn, "nobody", role="member")


@pytest.mark.parametrize("change", [{"role": "member"}, {"active": False}])
def test_update_user_refuses_to_remove_last_active_admin(conn, change):
    add_user(conn, id="a", name="A", role="admin")
    add_user(conn, id="b", name="B", role="admin")
    update_user(conn, "b", active=False)
    with pytest.raises(LastAdmin, match="There must be at least one active admin."):
        update_user(conn, "a", **change)
    a = get_user(conn, "a")
    assert (a["role"], a["active"]) == ("admin", 1)
    assert not conn.in_transaction
    assert update_user(conn, "a", role="admin", active=True)["role"] == "admin"
    update_user(conn, "b", active=True)
    update_user(conn, "a", **change)


def test_concurrent_demotion_of_last_two_admins_leaves_one(tmp_path, monkeypatch):
    path = str(tmp_path / "admins.db")
    init = connect(path)
    init_schema(init)
    add_user(init, id="a", name="A", role="admin")
    add_user(init, id="b", name="B", role="admin")
    init.close()
    original = db.get_user

    def slow_get_user(conn, id):
        row = original(conn, id)
        time.sleep(0.1)
        return row

    monkeypatch.setattr(db, "get_user", slow_get_user)
    barrier = threading.Barrier(2)

    def demote(target):
        c = connect(path)
        try:
            barrier.wait()
            update_user(c, target, role="member")
            return "demoted"
        except LastAdmin:
            return "refused"
        finally:
            c.close()

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(demote, ["a", "b"]))
    assert sorted(results) == ["demoted", "refused"]
    check = connect(path)
    try:
        admins = check.execute("SELECT id FROM users WHERE role='admin' AND active=1").fetchall()
    finally:
        check.close()
    assert len(admins) == 1


def test_deactivation_revokes_open_tokens_of_that_user(conn):
    add_user(conn, id="boss", name="Boss", role="admin")
    add_user(conn, id="m", name="M", role="member")
    token, _ = create_token(conn, owner_id="m", label="laptop")
    _, old = create_token(conn, owner_id="m", label="old")
    conn.execute("UPDATE api_tokens SET revoked_at='2000-01-01' WHERE id=?", (old["id"],))
    boss_token, _ = create_token(conn, owner_id="boss", label="ci")
    update_user(conn, "m", active=False)
    revoked = {t["label"]: t["revoked_at"] for t in list_tokens(conn)}
    assert revoked["laptop"] is not None
    assert revoked["old"] == "2000-01-01"
    assert revoked["ci"] is None
    update_user(conn, "m", active=True)
    assert authenticate_token(conn, token) is None
    assert authenticate_token(conn, boss_token)["id"] == "boss"


def test_bootstrap_admin_only_on_empty_table(conn):
    assert bootstrap_admin(conn, "boss@example.com") is True
    boss = get_user(conn, "boss@example.com")
    assert (boss["name"], boss["role"], boss["active"]) == ("boss", "admin", 1)
    assert bootstrap_admin(conn, "boss@example.com") is False
    assert bootstrap_admin(conn, "other@example.com") is False
    assert [u["id"] for u in list_users(conn)] == ["boss@example.com"]


def test_bootstrap_admin_name_falls_back_to_the_whole_id(conn):
    assert bootstrap_admin(conn, "@example.com") is True
    assert get_user(conn, "@example.com")["name"] == "@example.com"


def test_bootstrap_admin_skips_table_with_only_members(conn):
    add_user(conn, id="m", name="M", role="member")
    assert bootstrap_admin(conn, "boss@example.com") is False
    assert get_user(conn, "boss@example.com") is None


def test_token_round_trip_stores_only_a_digest(conn):
    add_user(conn, id="m@example.com", name="M", role="member")
    token, row = create_token(conn, owner_id="m@example.com", label="laptop")
    assert token.startswith("vf_")
    public_id, secret = token.removeprefix("vf_").split(".")
    assert len(public_id) == 12 and row["id"] == public_id
    assert (row["owner_id"], row["label"], row["revoked_at"]) == ("m@example.com", "laptop", None)
    assert "digest" not in row.keys()
    assert authenticate_token(conn, token)["name"] == "M"
    stored = conn.execute("SELECT * FROM api_tokens").fetchone()
    assert stored["digest"] == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in " ".join(str(v) for v in stored)


def test_authenticate_token_refuses_wrong_secret(conn):
    add_user(conn, id="m", name="M", role="member")
    token, _ = create_token(conn, owner_id="m", label="a")
    _, other = create_token(conn, owner_id="m", label="b")
    secret = token.split(".")[1]
    assert authenticate_token(conn, token + "x") is None
    assert authenticate_token(conn, f"vf_{other['id']}.{secret}") is None


def test_authenticate_token_refuses_revoked(conn):
    add_user(conn, id="m", name="M", role="member")
    token, row = create_token(conn, owner_id="m", label="a")
    kept, _ = create_token(conn, owner_id="m", label="b")
    assert revoke_token(conn, row["id"]) is True
    assert revoke_token(conn, row["id"]) is False
    assert revoke_token(conn, "missing") is False
    assert authenticate_token(conn, token) is None
    assert authenticate_token(conn, kept)["id"] == "m"


def test_authenticate_token_refuses_inactive_owner(conn):
    add_user(conn, id="m", name="M", role="member")
    token, _ = create_token(conn, owner_id="m", label="a")
    conn.execute("UPDATE users SET active=0 WHERE id='m'")
    assert authenticate_token(conn, token) is None


def test_authenticate_token_returns_none_for_malformed_strings(conn):
    add_user(conn, id="m", name="M", role="member")
    token, _ = create_token(conn, owner_id="m", label="a")
    body = token.removeprefix("vf_")
    malformed = [
        "",
        "vf_",
        "vf_.",
        "vf_.x",
        body,
        f"vx_{body}",
        f"vf_{body.split('.')[0]}",
        f"{token}.x",
        f"Bearer {token}",
        "vf_\x00.\u00ff",
    ]
    assert [authenticate_token(conn, t) for t in malformed] == [None] * len(malformed)


def test_list_tokens_filters_by_owner_and_never_returns_digest(conn):
    add_user(conn, id="a", name="A", role="admin")
    add_user(conn, id="m", name="M", role="member")
    create_token(conn, owner_id="a", label="ci")
    create_token(conn, owner_id="m", label="laptop")
    assert {t["label"] for t in list_tokens(conn)} == {"ci", "laptop"}
    assert [t["label"] for t in list_tokens(conn, "m")] == ["laptop"]
    assert all("digest" not in t.keys() for t in list_tokens(conn))


def test_create_token_refuses_unknown_or_inactive_owner(conn):
    add_user(conn, id="m", name="M", role="member")
    conn.execute("UPDATE users SET active=0 WHERE id='m'")
    for owner_id in ["m", "nobody"]:
        with pytest.raises(ValueError, match="unknown or inactive owner"):
            create_token(conn, owner_id=owner_id, label="x")
    assert list_tokens(conn) == []
    assert not conn.in_transaction


def test_api_tokens_owner_index_exists(conn):
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "api_tokens_owner" in indexes
