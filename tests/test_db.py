import asyncio
import hashlib
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import psycopg
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
    init_schema,
    latest_version,
    list_comments,
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


def _idle(conn):
    return conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


def _indexes(conn):
    rows = conn.execute("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")
    return {r["indexname"] for r in rows}


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
    assert _idle(conn)


def test_plan_snapshots_inherit_replace_and_remove(conn):
    original = "  # Návrh 🦊\n\nKeep exact whitespace.\r\n  "
    replacement = "# Replacement\nA different approach.\n"
    kwargs = {"id": "r1", "project": "p", "title": "T", "risk": "low", "body": "body"}
    results = [
        push_report(conn, **kwargs, plan=original),
        push_report(conn, **kwargs),
        push_report(conn, **kwargs, plan=replacement),
        push_report(conn, **kwargs, plan=None, inherit_plan=False),
        push_report(conn, **kwargs),
    ]
    assert results == [("r1", n) for n in range(1, 6)]
    versions = list_versions(conn, "r1")
    assert [v["plan"] for v in versions] == [original, original, replacement, None, None]
    assert [v["body"] for v in versions] == ["body"] * 5
    assert [v["version"] for v in versions] == [1, 2, 3, 4, 5]
    assert db.get_version(conn, "r1", 1)["plan"] == original
    for id, plan_args in [("omitted", {}), ("removed", {"plan": None, "inherit_plan": False})]:
        push_report(conn, **(kwargs | {"id": id}), **plan_args)
        assert db.get_version(conn, id, 1)["plan"] is None


def test_fresh_versions_plan_is_nullable_text(conn):
    column = conn.execute(
        "SELECT data_type, is_nullable FROM information_schema.columns"
        " WHERE table_schema=current_schema() AND table_name='versions' AND column_name='plan'"
    ).fetchone()
    assert column == {"data_type": "text", "is_nullable": "YES"}


def test_init_schema_adds_plan_to_legacy_versions(conn):
    push_report(conn, project="p", title="T", risk="low", body="legacy body", id="r1")
    signature = sign_version(conn, report_id="r1", version=1, signer_id="u1", signer_name="Ann")
    conn.execute("ALTER TABLE versions DROP COLUMN IF EXISTS plan")
    init_schema(conn)
    init_schema(conn)
    version = db.get_version(conn, "r1", 1)
    assert version["body"] == "legacy body"
    assert version["plan"] is None
    assert get_signature(conn, "r1", 1) == signature


def test_init_schema_does_not_wait_for_readers_of_versions_plan(new_database_url):
    url = new_database_url()
    conn, reader = connect(url), connect(url)
    init_schema(conn)
    try:
        assert conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema()"
            " AND table_name='versions' AND column_name='plan'"
        ).fetchone()
        with reader.transaction():
            reader.execute("SELECT * FROM versions")
            started = time.monotonic()
            init_schema(conn)
            assert time.monotonic() - started < 1
    finally:
        conn.close()
        reader.close()


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
    return conn.execute("SELECT resolved_at FROM comments WHERE id=%s", (id,)).fetchone()[
        "resolved_at"
    ]


def test_resolve_twice_keeps_first_time_and_reply_reopens(conn):
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    kw = {"report_id": "r1", "version": 1, "author_id": "u1", "author_name": "Alice"}
    root = add_comment(conn, body="?", **kw)
    resolve_thread(conn, "r1", root["id"])
    conn.execute("UPDATE comments SET resolved_at='2000-01-01' WHERE id=%s", (root["id"],))
    assert resolve_thread(conn, "r1", root["id"]) == root["id"]
    assert _resolved_at(conn, root["id"]) == "2000-01-01"
    add_comment(conn, body="still wrong", parent_id=root["id"], **kw)
    assert _resolved_at(conn, root["id"]) is None


def test_reply_with_reopen_false_keeps_the_thread_resolved(conn):
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    kw = {"report_id": "r1", "version": 1, "author_id": "u1", "author_name": "Alice"}
    root = add_comment(conn, body="?", **kw)
    resolve_thread(conn, "r1", root["id"], by="u1")
    reply = add_comment(conn, body="fixed", parent_id=root["id"], reopen=False, **kw)
    assert reply["parent_id"] == root["id"]
    assert _resolved_at(conn, root["id"]) is not None


def test_list_comments_orders_by_id(conn):
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    kw = {"report_id": "r1", "version": 1, "author_id": "u1", "author_name": "Alice"}
    root = add_comment(conn, body="?", **kw)
    reply = add_comment(conn, body="!", parent_id=root["id"], **kw)
    conn.execute("UPDATE comments SET created_at='2000-01-01' WHERE id=%s", (reply["id"],))
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
    assert conn.execute("SELECT COUNT(*) AS n FROM verdicts").fetchone()["n"] == 0
    assert _idle(conn)


def test_schema_checks_and_indexes(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        push_report(conn, project="p", title="T", risk="extreme", body="b")
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    with pytest.raises(psycopg.errors.CheckViolation):
        add_verdict(
            conn, report_id="r1", version=1, reviewer_id="u", reviewer_name="U", verdict="maybe"
        )
    assert {"comments_report_parent", "verdicts_report", "reports_project"} <= _indexes(conn)


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


def test_push_by_an_owner_to_an_unowned_report_is_refused(conn):
    push_report(conn, project="p", title="T", risk="low", body="v1", id="r1")
    with pytest.raises(db.NoOwner):
        push_report(conn, project="p", title="T", risk="low", body="v2", id="r1", owner_id="a")
    assert (get_report(conn, "r1")["author_id"], latest_version(conn, "r1")) == (None, 1)
    assert _idle(conn)


def test_list_reports_newest_first_with_latest_version(conn):
    push_report(conn, project="p", title="First", risk="low", body="a", id="r1")
    push_report(conn, project="p", title="Second", risk="low", body="b", id="r2")
    push_report(conn, project="other", title="Elsewhere", risk="low", body="c", id="r3")
    conn.execute("UPDATE reports SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = 'r1'")
    conn.execute("UPDATE reports SET created_at = '2026-01-02T00:00:00+00:00' WHERE id = 'r2'")

    rows = [r for r in list_reports(conn) if r["project"] == "p"]
    assert [r["id"] for r in rows] == ["r2", "r1"]
    assert [r["latest_version"] for r in rows] == [1, 1]
    assert [r["open_threads"] for r in rows] == [0, 0]

    push_report(conn, project="p", title="First", risk="low", body="a2", id="r1")
    add_comment(conn, report_id="r1", version=2, author_id="u1", author_name="Alice", body="?")
    by_id = {r["id"]: r for r in list_reports(conn)}
    assert by_id["r1"]["latest_version"] == 2
    assert by_id["r1"]["open_threads"] == 1
    assert by_id["r2"]["open_threads"] == 0


def test_list_reports_all_projects_and_latest_signature(conn):
    push_report(conn, project="p", title="A", risk="low", body="v1", id="a")
    push_report(conn, project="q", title="B", risk="low", body="v1", id="b")
    db.sign_version(conn, report_id="a", version=1, signer_id="alice", signer_name="Alice")
    push_report(conn, project="p", title="A", risk="low", body="v2", id="a")

    rows = {r["id"]: r for r in list_reports(conn)}
    assert set(rows) == {"a", "b"}
    assert (rows["a"]["signer_id"], rows["a"]["signed_at"]) == (None, None)
    assert (rows["b"]["signer_id"], rows["b"]["signed_at"]) == (None, None)

    signature = db.sign_version(conn, report_id="a", version=2, signer_id="alice", signer_name="A")
    rows = {r["id"]: r for r in list_reports(conn)}
    assert rows["a"]["signer_id"] == "alice"
    assert rows["a"]["signed_at"] == signature["created_at"]


def test_rows_by_report_ignore_reports_outside_the_loaded_ids(conn):
    for id in ("a", "b"):
        push_report(conn, project="p", title=id, risk="low", body="v1", id=id, reviewers=["x"])
        db.add_verdict(
            conn, report_id=id, version=1, reviewer_id="x", reviewer_name="X", verdict="approve"
        )
    assert [v["report_id"] for v in db.verdicts_by_report(conn, ["a"])["a"]] == ["a"]
    assert list(db.verdicts_by_report(conn, ["a"])) == ["a"]
    assert db.requests_by_report(conn, ["a"]) == {"a": ["x"]}
    assert db.requests_by_report(conn, ["a", "none"]) == {"a": ["x"], "none": []}
    assert db.verdicts_by_report(conn, []) == {}


def test_rows_by_report_take_any_number_of_ids(conn):
    push_report(conn, project="p", title="a", risk="low", body="v1", id="a")
    ids = [f"r{n}" for n in range(40000)] + ["a"]
    assert db.requests_by_report(conn, ids)["a"] == []


def test_project_exists(conn):
    push_report(conn, project="zeta", title="T", risk="low", body="b")
    assert db.project_exists(conn, "zeta")
    assert not db.project_exists(conn, "alpha")


def test_get_conn_route_write_is_visible_to_other_connection(new_database_url):
    url = new_database_url()
    init = connect(url)
    init_schema(init)
    init.close()
    app = FastAPI()
    app.state.pool = db.open_pool(url, 2)
    app.state.pool_slots = asyncio.Semaphore(2)

    @app.post("/push")
    def push(conn: Annotated[db.Connection, Depends(get_conn)]):
        return push_report(conn, project="p", title="T", risk="low", body="b")

    try:
        assert TestClient(app).post("/push").json() == ["t", 1]
        other = connect(url)
        try:
            assert get_report(other, "t")["project"] == "p"
        finally:
            other.close()
    finally:
        app.state.pool.close()


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


def _resolved_by(conn, id):
    return conn.execute("SELECT resolved_by FROM comments WHERE id=%s", (id,)).fetchone()[
        "resolved_by"
    ]


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
    assert _idle(conn)


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
    promoted, revoked = update_user(conn, "m", role="admin")
    assert (promoted["role"], promoted["active"], revoked) == ("admin", 1, 0)
    assert promoted["updated_at"] != "2000-01-01"
    off, revoked = update_user(conn, "m", active=False)
    assert (off["role"], off["active"], revoked) == ("admin", 0, 0)
    assert update_user(conn, "m", active=True)[0]["active"] == 1
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
    assert _idle(conn)
    assert update_user(conn, "a", role="admin", active=True)[0]["role"] == "admin"
    update_user(conn, "b", active=True)
    update_user(conn, "a", **change)


def test_update_user_refuses_a_role_change_for_an_inactive_member(conn):
    add_user(conn, id="boss", name="Boss", role="admin")
    add_user(conn, id="m", name="M", role="member")
    update_user(conn, "m", active=False)
    with pytest.raises(db.InactiveMember):
        update_user(conn, "m", role="admin")
    assert get_user(conn, "m")["role"] == "member"
    assert _idle(conn)
    assert update_user(conn, "m", role="member", active=False)[0]["active"] == 0
    assert update_user(conn, "m", role="admin", active=True)[0]["role"] == "admin"


def test_deactivation_revokes_open_tokens_of_that_user(conn):
    add_user(conn, id="boss", name="Boss", role="admin")
    add_user(conn, id="m", name="M", role="member")
    token, _ = create_token(conn, owner_id="m", label="laptop")
    _, old = create_token(conn, owner_id="m", label="old")
    conn.execute("UPDATE api_tokens SET revoked_at='2000-01-01' WHERE id=%s", (old["id"],))
    boss_token, _ = create_token(conn, owner_id="boss", label="ci")
    assert update_user(conn, "m", active=False)[1] == 1
    assert update_user(conn, "m", active=False)[1] == 0
    revoked = {t["label"]: t["revoked_at"] for t in list_tokens(conn)}
    assert revoked["laptop"] is not None
    assert revoked["old"] == "2000-01-01"
    assert revoked["ci"] is None
    assert update_user(conn, "m", active=True)[1] == 0
    assert authenticate_token(conn, token) == (None, "revoked")
    assert authenticate_token(conn, boss_token)[0]["id"] == "boss"


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
    assert authenticate_token(conn, token)[0]["name"] == "M"
    stored = conn.execute("SELECT * FROM api_tokens").fetchone()
    assert stored["digest"] == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in " ".join(str(v) for v in stored.values())


def test_create_token_with_a_used_nonce_creates_nothing(conn):
    add_user(conn, id="m", name="M", role="member")
    add_user(conn, id="n", name="N", role="member")
    create_token(conn, owner_id="m", label="a", nonce="x1")
    with pytest.raises(db.AlreadyCreated):
        create_token(conn, owner_id="m", label="again", nonce="x1")
    create_token(conn, owner_id="n", label="other owner", nonce="x1")
    create_token(conn, owner_id="m", label="no nonce")
    create_token(conn, owner_id="m", label="no nonce")
    assert sorted(t["label"] for t in list_tokens(conn)) == [
        "a",
        "no nonce",
        "no nonce",
        "other owner",
    ]
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO api_tokens (id, digest, owner_id, label, created_at, nonce)"
            " VALUES ('z', 'd', 'm', 'l', 'now', 'x1')"
        )


def test_authenticate_token_refuses_wrong_secret(conn):
    add_user(conn, id="m", name="M", role="member")
    token, _ = create_token(conn, owner_id="m", label="a")
    _, other = create_token(conn, owner_id="m", label="b")
    secret = token.split(".")[1]
    assert authenticate_token(conn, token + "x") == (None, "unknown")
    assert authenticate_token(conn, f"vf_{other['id']}.{secret}") == (None, "unknown")
    assert authenticate_token(conn, f"vf_nope.{secret}") == (None, "unknown")


def test_authenticate_token_refuses_revoked(conn):
    add_user(conn, id="m", name="M", role="member")
    token, row = create_token(conn, owner_id="m", label="a")
    kept, _ = create_token(conn, owner_id="m", label="b")
    assert revoke_token(conn, row["id"]) is True
    assert revoke_token(conn, row["id"]) is False
    assert revoke_token(conn, "missing") is False
    assert authenticate_token(conn, token) == (None, "revoked")
    assert authenticate_token(conn, token + "x") == (None, "unknown")
    assert authenticate_token(conn, kept)[0]["id"] == "m"


def test_authenticate_token_refuses_inactive_owner(conn):
    add_user(conn, id="m", name="M", role="member")
    token, _ = create_token(conn, owner_id="m", label="a")
    conn.execute("UPDATE users SET active=0 WHERE id='m'")
    assert authenticate_token(conn, token) == (None, "inactive")


def test_authenticate_token_refuses_a_token_whose_owner_row_is_gone(conn):
    add_user(conn, id="m", name="M", role="member")
    token, _ = create_token(conn, owner_id="m", label="a")
    conn.execute("ALTER TABLE api_tokens DROP CONSTRAINT api_tokens_owner_id_fkey")
    conn.execute("DELETE FROM users WHERE id='m'")
    assert authenticate_token(conn, token) == (None, "inactive")


def test_authenticate_token_refuses_malformed_strings(conn):
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
    reasons = [authenticate_token(conn, t) for t in malformed]
    assert reasons == [
        (None, "unknown" if t in ("vf_.", "vf_.x", "vf_\x00.\u00ff") else "malformed")
        for t in malformed
    ]


def test_list_tokens_filters_by_owner_and_never_returns_digest(conn):
    add_user(conn, id="a", name="A", role="admin")
    add_user(conn, id="m", name="M", role="member")
    create_token(conn, owner_id="a", label="ci")
    create_token(conn, owner_id="m", label="laptop")
    assert {t["label"] for t in list_tokens(conn)} == {"ci", "laptop"}
    assert [t["label"] for t in list_tokens(conn, "m")] == ["laptop"]
    assert all("digest" not in t.keys() for t in list_tokens(conn))


def test_token_last_used_is_throttled(conn):
    add_user(conn, id="m", name="M", role="member")
    token, row = create_token(conn, owner_id="m", label="a")

    def last_used():
        return conn.execute(
            "SELECT last_used_at FROM api_tokens WHERE id=%s", (row["id"],)
        ).fetchone()["last_used_at"]

    def seconds_ago(n):
        return (datetime.now(UTC) - timedelta(seconds=n)).isoformat(timespec="seconds")

    assert authenticate_token(conn, token)[0]["id"] == "m"
    assert last_used() is None
    assert authenticate_token(conn, token, touch=True)[0]["id"] == "m"
    assert last_used() is not None
    recent = seconds_ago(30)
    conn.execute("UPDATE api_tokens SET last_used_at=%s", (recent,))
    authenticate_token(conn, token, touch=True)
    assert last_used() == recent
    old = seconds_ago(61)
    conn.execute("UPDATE api_tokens SET last_used_at=%s", (old,))
    authenticate_token(conn, token, touch=True)
    assert last_used() > old
    assert list_tokens(conn)[0]["last_used_at"] == last_used()
    assert authenticate_token(conn, token + "x", touch=True) == (None, "unknown")


def test_token_auth_survives_a_failed_last_used_write(conn, monkeypatch, caplog):
    add_user(conn, id="m", name="M", role="member")
    token, row = create_token(conn, owner_id="m", label="a")

    def locked(*args):
        raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")

    monkeypatch.setattr(db, "_touch_token", locked)
    assert authenticate_token(conn, token, touch=True)[0]["id"] == "m"
    assert f"token last_used not saved id={row['id']}" in caplog.text


def test_token_last_used_gives_up_fast_when_the_row_is_locked(new_database_url, caplog):
    url = new_database_url()
    conn, other = connect(url), connect(url)
    init_schema(conn)
    add_user(conn, id="m", name="M", role="member")
    token, row = create_token(conn, owner_id="m", label="a")
    try:
        with other.transaction():
            other.execute("SELECT 1 FROM api_tokens WHERE id=%s FOR UPDATE", (row["id"],))
            started = time.monotonic()
            assert authenticate_token(conn, token, touch=True)[0]["id"] == "m"
            assert time.monotonic() - started < 1
        assert f"token last_used not saved id={row['id']}" in caplog.text
        assert _idle(conn)
        assert conn.execute("SHOW lock_timeout").fetchone()["lock_timeout"] == "0"
    finally:
        conn.close()
        other.close()


def test_create_token_refuses_unknown_or_inactive_owner(conn):
    add_user(conn, id="m", name="M", role="member")
    conn.execute("UPDATE users SET active=0 WHERE id='m'")
    for owner_id in ["m", "nobody"]:
        with pytest.raises(ValueError, match="unknown or inactive owner"):
            create_token(conn, owner_id=owner_id, label="x")
    assert list_tokens(conn) == []
    assert _idle(conn)


def test_api_tokens_owner_index_exists(conn):
    assert {"api_tokens_owner", "api_tokens_owner_nonce", "reports_author"} <= _indexes(conn)


def test_bootstrap_admin_name_is_capped_at_60(conn):
    assert bootstrap_admin(conn, "a" * 80 + "@example.com") is True
    assert get_user(conn, "a" * 80 + "@example.com")["name"] == "a" * 60


def test_init_schema_is_idempotent(conn):
    init_schema(conn)
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    init_schema(conn)
    assert latest_version(conn, "r1") == 1


def test_init_schema_adds_requested_by_to_an_old_table(conn):
    conn.execute("ALTER TABLE review_requests DROP COLUMN requested_by")
    init_schema(conn)
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    db.add_review_request(conn, "r1", "b", requested_by="a")
    assert db.review_requesters(conn, "r1") == {"b": "a"}


def test_init_schema_does_not_wait_for_readers_of_review_requests(new_database_url):
    url = new_database_url()
    conn, reader = connect(url), connect(url)
    init_schema(conn)
    try:
        with reader.transaction():
            reader.execute("SELECT * FROM review_requests")
            started = time.monotonic()
            init_schema(conn)
            assert time.monotonic() - started < 1
    finally:
        conn.close()
        reader.close()


def test_comment_and_verdict_ids_come_from_returning(conn):
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    kw = {"report_id": "r1", "version": 1, "author_id": "u", "author_name": "U"}
    first, second = add_comment(conn, body="a", **kw), add_comment(conn, body="b", **kw)
    assert second["id"] > first["id"] and second["body"] == "b"
    verdict = add_verdict(
        conn, report_id="r1", version=1, reviewer_id="u", reviewer_name="U", verdict="approve"
    )
    assert verdict["id"] >= 1 and verdict["verdict"] == "approve"


def test_text_ordering_is_bytewise(conn):
    for id, name in (("b", "beta"), ("A", "Alpha"), ("a", "alpha")):
        conn.execute(
            "INSERT INTO users (id, name, role, created_at, updated_at)"
            " VALUES (%s, %s, 'member', 'now', 'now')",
            (id, name),
        )
    assert [u["name"] for u in list_users(conn)] == ["Alpha", "alpha", "beta"]
    reviewers = ["beta", "alpha", "Alpha"]
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1", reviewers=reviewers)
    assert db.requested_reviewers(conn, "r1") == ["Alpha", "alpha", "beta"]
    assert db.requests_by_report(conn, ["r1"]) == {"r1": ["Alpha", "alpha", "beta"]}


def test_list_tokens_newest_first_within_one_second(conn, monkeypatch):
    monkeypatch.setattr(db, "_now", lambda: "2026-01-01T00:00:00+00:00")
    add_user(conn, id="m", name="M", role="member")
    for label in ("a", "b", "c"):
        create_token(conn, owner_id="m", label=label)
    assert [t["label"] for t in list_tokens(conn, "m")] == ["c", "b", "a"]


def test_list_reports_newest_first_within_one_second(conn, monkeypatch):
    monkeypatch.setattr(db, "_now", lambda: "2026-01-01T00:00:00+00:00")
    for id in ("r2", "r3", "r1"):
        push_report(conn, project="p", title="T", risk="low", body="b", id=id)
    assert [r["id"] for r in list_reports(conn)] == ["r1", "r3", "r2"]


def test_failed_write_leaves_the_connection_usable(conn):
    push_report(conn, project="p", title="T", risk="low", body="b", id="r1")
    with pytest.raises(StaleVersion):
        add_comment(
            conn,
            report_id="r1",
            version=1,
            author_id="u",
            author_name="U",
            body="x",
            expected_version=2,
        )
    assert _idle(conn)
    assert latest_version(conn, "r1") == 1


def test_token_auth_does_not_hide_a_lost_connection_during_the_touch(conn, monkeypatch):
    add_user(conn, id="m", name="M", role="member")
    token, _ = create_token(conn, owner_id="m", label="a")

    def lost(*args):
        raise psycopg.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(db, "_touch_token", lost)
    with pytest.raises(psycopg.OperationalError, match="server closed"):
        authenticate_token(conn, token, touch=True)


def _plan_push(conn, **kwargs):
    return push_report(
        conn,
        **(
            {
                "id": "plan",
                "project": "p",
                "title": "Plan",
                "risk": "low",
                "body": None,
                "body_supplied": False,
            }
            | kwargs
        ),
    )


def _plan_sign(conn, revision=1, snapshot=1, signer="owner"):
    return sign_version(
        conn,
        report_id="plan",
        target="plan",
        version=revision,
        expected_version=snapshot,
        signer_id=signer,
        signer_name=signer,
    )


def _plan_verdict(conn, revision=1, snapshot=1, reviewer="reviewer", verdict="approve"):
    return add_verdict(
        conn,
        report_id="plan",
        target="plan",
        version=revision,
        expected_version=snapshot,
        reviewer_id=reviewer,
        reviewer_name=reviewer,
        verdict=verdict,
    )


def _approved_plan(conn):
    _plan_push(conn, workflow="plan_first", plan="# Návrh\r\nExact  \n")
    _plan_sign(conn)
    _plan_verdict(conn)


def test_plan_schema_upgrade_preserves_legacy_records(new_database_url):
    with connect(new_database_url()) as conn:
        conn.execute("""
            CREATE TABLE reports (
                id TEXT PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL,
                risk TEXT NOT NULL, ticket_url TEXT, links TEXT NOT NULL DEFAULT '[]',
                author TEXT NOT NULL, author_id TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, agent TEXT, model TEXT, session_url TEXT,
                seq BIGINT GENERATED ALWAYS AS IDENTITY);
            CREATE TABLE versions (
                report_id TEXT NOT NULL REFERENCES reports(id), version INTEGER NOT NULL,
                body TEXT NOT NULL, plan TEXT, created_at TEXT NOT NULL, "commit" TEXT,
                PRIMARY KEY (report_id, version));
            CREATE TABLE signatures (
                report_id TEXT NOT NULL REFERENCES reports(id), version INTEGER NOT NULL,
                signer_id TEXT NOT NULL, signer_name TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (report_id, version));
            CREATE TABLE comments (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                report_id TEXT NOT NULL REFERENCES reports(id), version INTEGER NOT NULL,
                parent_id BIGINT REFERENCES comments(id), section TEXT, quote TEXT,
                author_id TEXT NOT NULL, author_name TEXT NOT NULL, body TEXT NOT NULL,
                created_at TEXT NOT NULL, resolved_at TEXT, blocking INTEGER NOT NULL DEFAULT 1,
                prefix TEXT, suffix TEXT, resolved_by TEXT);
            CREATE TABLE verdicts (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                report_id TEXT NOT NULL REFERENCES reports(id), version INTEGER NOT NULL,
                reviewer_id TEXT NOT NULL, reviewer_name TEXT NOT NULL, verdict TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
            INSERT INTO reports (id, project, title, risk, author, created_at, updated_at)
                VALUES ('r', 'p', 'T', 'low', 'agent', 'then', 'then');
            INSERT INTO signatures VALUES ('r', 1, 'Owner', 'Owner', 'signed');
            INSERT INTO verdicts (report_id, version, reviewer_id, reviewer_name, verdict,
                                  created_at)
                VALUES ('r', 1, 'Reviewer', 'Reviewer', 'approve', 'approved');
            INSERT INTO comments (report_id, version, author_id, author_name, body, created_at)
                VALUES ('r', 1, 'Reviewer', 'Reviewer', 'root', 'commented');
            INSERT INTO comments (report_id, version, parent_id, author_id, author_name, body,
                                  created_at)
                VALUES ('r', 1, 1, 'agent', 'agent', 'reply', 'replied');
        """)
        a, b = "# Návrh 🦊\r\ntext  \n", "# Návrh 🦊\ntext\n"
        for n, plan in enumerate([a, a, b, None, a], 1):
            conn.execute(
                "INSERT INTO versions VALUES (%s, %s, %s, %s, %s, %s)",
                ("r", n, f"body{n}", plan, f"time{n}", f"commit{n}"),
            )
        before = {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("reports", "versions", "comments", "signatures", "verdicts")
        }
        init_schema(conn)
        init_schema(conn)
        assert [v["plan_revision"] for v in list_versions(conn, "r")] == [1, 1, 3, None, 5]
        for table, old_rows in before.items():
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            assert len(rows) == len(old_rows)
            for old in old_rows:
                assert any(all(row[key] == value for key, value in old.items()) for row in rows)
        assert get_report(conn, "r")["workflow"] == "artifact"
        for table in ("comments", "signatures", "verdicts"):
            assert {r["target"] for r in conn.execute(f"SELECT * FROM {table}")} == {
                "implementation"
            }
        assert db.target_document(conn, "r", 2, "plan") == (1, a)
        assert db.target_document(conn, "r", 4, "plan") is None
        _check_plan_schema_constraints(conn)


def test_plan_schema_constraints_and_target_signature_keys(conn):
    _check_plan_schema_constraints(conn)


def _check_plan_schema_constraints(conn):
    _plan_push(conn, workflow="plan_first", plan="A")
    _plan_sign(conn)
    conn.execute(
        "INSERT INTO signatures VALUES ('plan', 1, 'other', 'Other', 'now', 'implementation')"
    )
    assert len(conn.execute("SELECT * FROM signatures WHERE report_id='plan'").fetchall()) == 2
    bad_versions = [
        (None, None, None),
        ("", "A", 1),
        ("x", "", 1),
        ("x", "A", None),
        ("x", None, 1),
        ("x", "A", 0),
        ("x", "A", 3),
    ]
    for body, plan, pointer in bad_versions:
        with pytest.raises(psycopg.IntegrityError), conn.transaction():
            conn.execute(
                "INSERT INTO versions (report_id, version, body, plan, plan_revision,"
                " created_at) VALUES (%s, 2, %s, %s, %s, %s)",
                ("plan", body, plan, pointer, "now"),
            )
    with pytest.raises(psycopg.IntegrityError), conn.transaction():
        conn.execute("UPDATE reports SET workflow='unknown'")
    add_comment(
        conn,
        report_id="plan",
        target="plan",
        version=1,
        author_id="r",
        author_name="R",
        body="concern",
        blocking=False,
    )
    _plan_verdict(conn)
    for table in ("signatures", "comments", "verdicts"):
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(f"UPDATE {table} SET target='unknown' WHERE report_id='plan'")
    assert len([r for r in list_reports(conn) if r["id"] == "plan"]) == 1
    push_report(conn, project="p", title="Other", risk="low", body="x", id="other")
    _plan_push(conn, plan="B", expected_version=1)
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        conn.execute(
            "INSERT INTO versions (report_id, version, body, plan, plan_revision,"
            " created_at) VALUES ('other', 3, 'x', 'A', 2, 'now')"
        )


def test_plan_revision_approval_survives_implementation_push(conn):
    _approved_plan(conn)
    state = db.plan_review_state(conn, "plan")
    assert state["status"] == "approved" and state["revision"] == 1
    _plan_push(conn, body="implementation", body_supplied=True, expected_version=1, commit="abc")
    assert db.target_document(conn, "plan", 2, "implementation") == (2, "implementation")
    assert db.plan_review_state(conn, "plan") == state
    assert get_signature(conn, "plan", 2) is None
    assert db.list_verdicts(conn, "plan") == []
    assert len(db.list_verdicts(conn, "plan", target="plan")) == 1
    _plan_push(conn, plan="# Návrh\r\nExact  \n", expected_version=2)
    snapshot = db.get_version(conn, "plan", 3)
    assert (snapshot["plan_revision"], snapshot["body"], snapshot["commit"]) == (
        1,
        "implementation",
        "abc",
    )


def test_plan_edit_requires_new_signature_and_verdict(conn):
    _approved_plan(conn)
    _plan_push(conn, body="body", body_supplied=True, expected_version=1)
    sign_version(conn, report_id="plan", version=2, signer_id="owner", signer_name="O")
    _plan_push(conn, plan="B", expected_version=2)
    state = db.plan_review_state(conn, "plan")
    assert (state["revision"], state["signature"], state["status"]) == (3, None, "unsigned")
    assert state["latest_verdicts"] == {}
    assert get_signature(conn, "plan", 2) is not None
    assert db.plan_review_state(conn, "plan", 1)["status"] == "approved"
    _plan_sign(conn, 3, 3)
    assert db.plan_review_state(conn, "plan")["status"] == "awaiting_review"
    _plan_verdict(conn, 3, 3)
    assert db.plan_review_state(conn, "plan")["status"] == "approved"
    assert get_signature(conn, "plan", 3) is None


def test_plan_revert_is_new_revision(conn):
    for plan in ["A", "A", "B", None, "A"]:
        push_report(
            conn,
            id="r",
            project="p",
            title="T",
            risk="low",
            body="b",
            plan=plan,
            inherit_plan=False,
        )
    assert [v["plan_revision"] for v in list_versions(conn, "r")] == [1, 1, 3, None, 5]
    assert db.plan_review_state(conn, "r") is None


@pytest.mark.parametrize(
    "state",
    ["unsigned", "awaiting_review", "request_changes", "blocker", "same_signer", "changed_plan"],
)
def test_plan_attachment_gate(conn, state):
    _plan_push(conn, workflow="plan_first", plan="A")
    if state != "unsigned":
        _plan_sign(conn)
    if state in ("blocker", "same_signer", "changed_plan"):
        _plan_verdict(conn, reviewer="owner" if state == "same_signer" else "reviewer")
    if state == "request_changes":
        _plan_verdict(conn, verdict="request_changes")
    if state == "blocker":
        add_comment(
            conn,
            report_id="plan",
            target="plan",
            version=1,
            author_id="r",
            author_name="R",
            body="no",
        )
    before = get_report(conn, "plan")
    with pytest.raises(db.PlanNotApproved) as exc:
        _plan_push(
            conn,
            body="body",
            body_supplied=True,
            expected_version=1,
            title="must roll back",
            reviewers=["new"],
            **({"plan": "B"} if state == "changed_plan" else {}),
        )
    assert exc.value.plan_revision == (2 if state == "changed_plan" else 1)
    assert get_report(conn, "plan") == before
    assert db.requested_reviewers(conn, "plan") == []
    assert latest_version(conn, "plan") == 1


def test_plan_reopened_blocker_holds_and_resolution_restores(conn):
    _approved_plan(conn)
    root = add_comment(
        conn, report_id="plan", target="plan", version=1, author_id="r", author_name="R", body="no"
    )
    assert db.plan_review_state(conn, "plan")["status"] == "changes_requested"
    resolve_thread(conn, "plan", root["id"])
    assert db.plan_review_state(conn, "plan")["status"] == "approved"
    _plan_push(
        conn, body="body", body_supplied=True, plan="# Návrh\r\nExact  \n", expected_version=1
    )
    add_comment(
        conn, report_id="plan", version=2, author_id="r", author_name="R", body="body issue"
    )
    reopen_thread(conn, "plan", root["id"])
    assert db.plan_review_state(conn, "plan")["status"] == "changes_requested"
    for action in [
        lambda: sign_version(conn, report_id="plan", version=2, signer_id="owner", signer_name="O"),
        lambda: add_verdict(
            conn,
            report_id="plan",
            version=2,
            reviewer_id="r",
            reviewer_name="R",
            verdict="request_changes",
        ),
        lambda: _plan_push(conn, body="new", body_supplied=True, expected_version=2),
    ]:
        with pytest.raises(db.PlanNotApproved):
            action()
    assert get_signature(conn, "plan", 2) is None and db.list_verdicts(conn, "plan") == []
    set_blocking(conn, "plan", root["id"], False)
    assert db.plan_review_state(conn, "plan")["status"] == "approved"
    _plan_push(conn, plan="B", expected_version=2)
    _plan_sign(conn, 3, 3)
    _plan_verdict(conn, 3, 3)
    set_blocking(conn, "plan", root["id"], True)
    assert db.plan_review_state(conn, "plan")["open_blocking"] == 1
    assert db.plan_review_state(conn, "plan")["status"] == "changes_requested"
    later = add_comment(
        conn,
        report_id="plan",
        target="plan",
        version=3,
        author_id="r",
        author_name="R",
        body="later",
    )
    resolve_thread(conn, "plan", root["id"])
    assert db.plan_review_state(conn, "plan", 1)["status"] == "approved"
    assert db.plan_review_state(conn, "plan")["status"] == "changes_requested"
    resolve_thread(conn, "plan", later["id"])
    assert db.plan_review_state(conn, "plan")["status"] == "approved"


def test_plan_comments_inherit_target_and_guard_current_before_old_root(conn):
    _approved_plan(conn)
    root = add_comment(
        conn,
        report_id="plan",
        target="plan",
        version=1,
        author_id="r",
        author_name="R",
        body="no",
        section="same",
        quote="Exact",
        prefix="a",
        suffix="b",
        blocking=False,
    )
    _plan_push(conn, plan="B", expected_version=1)
    seen = []
    reply = add_comment(
        conn,
        report_id="plan",
        parent_id=root["id"],
        version=2,
        author_id="agent",
        author_name="agent",
        body="fixed",
        expected_version=2,
        validate=lambda markdown, revision: seen.append((markdown, revision)),
    )
    assert seen == [("B", 2)]
    assert (
        reply["target"],
        reply["version"],
        reply["section"],
        reply["quote"],
        reply["prefix"],
        reply["suffix"],
        reply["blocking"],
    ) == ("plan", 1, "same", "Exact", "a", "b", 0)
    assert db.list_comments(conn, "plan") == []
    assert len(db.list_comments(conn, "plan", target="plan")) == 2
    with pytest.raises(ValueError, match="target"):
        add_comment(
            conn,
            report_id="plan",
            parent_id=root["id"],
            target="implementation",
            author_id="agent",
            author_name="agent",
            body="wrong",
        )
    with pytest.raises(db.StaleRevision) as exc:
        add_comment(
            conn,
            report_id="plan",
            parent_id=root["id"],
            version=1,
            author_id="agent",
            author_name="agent",
            body="stale",
        )
    assert (exc.value.target, exc.value.current_revision) == ("plan", 2)
    with pytest.raises(ValueError, match="revision"):
        add_comment(
            conn, report_id="plan", target="plan", author_id="r", author_name="R", body="no"
        )


def test_plan_missing_implementation_and_read_only_artifact_plan(conn):
    _plan_push(conn, workflow="plan_first", plan="A")
    with pytest.raises(db.MissingDocument, match="implementation has not been attached"):
        add_comment(conn, report_id="plan", author_id="r", author_name="R", body="no")
    with pytest.raises(db.MissingDocument):
        sign_version(conn, report_id="plan", version=1, signer_id="o", signer_name="O")
    with pytest.raises(db.MissingDocument):
        add_verdict(
            conn, report_id="plan", version=1, reviewer_id="r", reviewer_name="R", verdict="approve"
        )
    push_report(conn, id="a", project="p", title="T", risk="low", body="body", plan="A")
    assert db.target_document(conn, "a", 1, "plan") == (1, "A")
    for action in [
        lambda: sign_version(
            conn,
            report_id="a",
            target="plan",
            version=1,
            expected_version=1,
            signer_id="o",
            signer_name="O",
        ),
        lambda: add_comment(
            conn, report_id="a", target="plan", version=1, author_id="r", author_name="R", body="no"
        ),
        lambda: add_verdict(
            conn,
            report_id="a",
            target="plan",
            version=1,
            reviewer_id="r",
            reviewer_name="R",
            verdict="approve",
        ),
    ]:
        with pytest.raises(ValueError, match="artifact"):
            action()


def test_plan_optimistic_update_conflict_and_invalid_pushes(conn):
    _approved_plan(conn)
    _plan_push(conn, plan="B", expected_version=1)
    with pytest.raises(StaleVersion) as exc:
        _plan_push(conn, plan="C", expected_version=1, reviewers=["no"], title="bad")
    assert exc.value.latest == 2
    for args in [
        dict(plan=None, inherit_plan=False),
        dict(body=None, body_supplied=True),
        dict(plan=" "),
        dict(plan="B", commit="abc"),
        dict(),
        dict(plan="B", body=" "),
        dict(plan="B", expected_version=None),
    ]:
        with pytest.raises(ValueError):
            _plan_push(conn, **({"expected_version": 2} | args))
    with pytest.raises(db.WorkflowMismatch, match="workflow cannot be changed"):
        _plan_push(conn, workflow="artifact", expected_version=2, body="b", body_supplied=True)
    assert latest_version(conn, "plan") == 2 and db.requested_reviewers(conn, "plan") == []
    assert get_report(conn, "plan")["title"] == "Plan"


def test_plan_list_rows_and_request_removal_use_both_targets(conn, monkeypatch):
    _approved_plan(conn)
    db.add_review_request(conn, "plan", "reviewer")
    _plan_push(conn, body="body", body_supplied=True, expected_version=1)
    sign_version(conn, report_id="plan", version=2, signer_id="owner", signer_name="O")
    add_verdict(
        conn,
        report_id="plan",
        version=2,
        reviewer_id="other",
        reviewer_name="Other",
        verdict="approve",
    )
    assert db.remove_review_request(conn, "plan", "reviewer") == "kept"
    monkeypatch.setattr(db, "plan_review_state", lambda *a, **k: pytest.fail("per-row query"))
    rows = list_reports(conn)
    assert len(rows) == 1
    row = rows[0]
    assert (row["workflow"], row["plan_revision"], row["has_body"], row["open_plan_blocking"]) == (
        "plan_first",
        1,
        True,
        0,
    )
    assert row["plan_signer_id"] == row["implementation_signer_id"] == row["signer_id"] == "owner"
    assert row["plan_signed_at"] and row["implementation_signed_at"] == row["signed_at"]
    assert {v["target"] for v in db.verdicts_by_report(conn, ["plan"])["plan"]} == {
        "plan",
        "implementation",
    }
    _plan_push(conn, plan="B", expected_version=2)
    assert db.remove_review_request(conn, "plan", "reviewer") == "removed"


def test_plan_human_ids_lowercase(conn):
    _plan_push(conn, workflow="plan_first", plan="A")
    _plan_sign(conn, signer="Owner")
    _plan_verdict(conn, reviewer="Reviewer")
    root = add_comment(
        conn,
        report_id="plan",
        target="plan",
        version=1,
        author_id="Reviewer",
        author_name="R",
        body="no",
    )
    resolve_thread(conn, "plan", root["id"], by="Owner")
    assert db.lowercase_human_ids(conn) >= 5
    assert get_signature(conn, "plan", 1, target="plan")["signer_id"] == "owner"
    assert db.list_verdicts(conn, "plan", target="plan")[0]["reviewer_id"] == "reviewer"
    assert db.list_comments(conn, "plan", target="plan")[0]["resolved_by"] == "owner"


def test_plan_signature_and_verdict_guard_snapshot_separately_from_plan_revision(conn):
    _plan_push(conn, workflow="plan_first", plan="A")
    _plan_push(conn, plan="A", expected_version=1)
    _plan_push(conn, plan="A", expected_version=2)
    with pytest.raises(StaleVersion) as exc:
        _plan_sign(conn, 1, 1)
    assert exc.value.latest == 3
    with pytest.raises(db.StaleRevision) as exc:
        _plan_sign(conn, 3, 3)
    assert (exc.value.target, exc.value.current_revision) == ("plan", 1)
    assert get_signature(conn, "plan", 1, target="plan") is None
    _plan_sign(conn, 1, 3)
    _plan_verdict(conn, 1, 3)
    assert db.plan_review_state(conn, "plan")["status"] == "approved"
    assert db.plan_review_state(conn, "plan", 1)["status"] == "approved"


def test_plan_comment_callback_refusal_leaves_resolution_and_rows_unchanged(conn):
    _approved_plan(conn)
    root = add_comment(
        conn,
        report_id="plan",
        target="plan",
        version=1,
        author_id="r",
        author_name="R",
        body="concern",
    )
    resolve_thread(conn, "plan", root["id"])
    before = db.list_comments(conn, "plan", target="plan")
    with pytest.raises(ValueError, match="quote missing"):
        add_comment(
            conn,
            report_id="plan",
            parent_id=root["id"],
            author_id="agent",
            author_name="agent",
            body="reply",
            validate=lambda markdown, revision: "quote missing",
        )
    assert db.list_comments(conn, "plan", target="plan") == before
    assert db.plan_review_state(conn, "plan")["status"] == "approved"


@pytest.mark.parametrize(
    "args",
    [
        dict(body=None),
        dict(body=" "),
        dict(plan=" ", body="b"),
        dict(workflow="invalid", body="b"),
        dict(workflow="plan_first", body="b", plan="A"),
        dict(workflow="plan_first", body=None),
        dict(workflow="plan_first", body=None, plan="A", commit="abc"),
        dict(workflow="plan_first", body=None, plan="A", expected_version=1),
    ],
)
def test_plan_invalid_creation_is_atomic(conn, args):
    with pytest.raises(ValueError):
        push_report(conn, id="invalid", project="p", title="T", risk="low", reviewers=["r"], **args)
    assert get_report(conn, "invalid") is None
    assert list_versions(conn, "invalid") == []
    assert db.requested_reviewers(conn, "invalid") == []


def test_plan_comment_invalid_explicit_target_is_not_omission(conn):
    push_report(conn, id="a", project="p", title="T", risk="low", body="body")
    with pytest.raises(ValueError, match="target"):
        add_comment(conn, report_id="a", target="", author_id="r", author_name="R", body="no")
    with pytest.raises(ValueError, match="artifact"):
        add_comment(
            conn, report_id="a", target="plan", version=1, author_id="r", author_name="R", body="no"
        )
    assert list_comments(conn, "a") == []


def test_plan_migration_lock_timeout_rolls_back_and_retry_preserves_data(new_database_url):
    url = new_database_url()
    with connect(url) as conn, connect(url) as reader:
        init_schema(conn)
        push_report(conn, id="r", project="p", title="T", risk="low", body="body", plan="A")
        conn.execute("ALTER TABLE reports DROP COLUMN workflow")
        conn.execute("ALTER TABLE versions DROP COLUMN plan_revision")
        conn.execute("ALTER TABLE versions DROP CONSTRAINT versions_content")
        conn.execute("ALTER TABLE versions ALTER COLUMN body SET NOT NULL")
        with reader.transaction():
            reader.execute("SELECT * FROM versions")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                init_schema(conn)
            columns = conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_schema=current_schema() AND table_name='reports'"
            ).fetchall()
            assert "workflow" not in {r["column_name"] for r in columns}
        init_schema(conn)
        assert db.get_version(conn, "r", 1)["body"] == "body"
        assert db.get_version(conn, "r", 1)["plan_revision"] == 1
