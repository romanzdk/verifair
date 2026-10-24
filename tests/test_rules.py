import pytest

from verifair.db import add_comment, add_verdict, connect, init_schema, push_report, resolve_thread
from verifair.render import build_threads
from verifair.rules import (
    can_approve,
    can_moderate,
    can_request_changes,
    compute_status,
    inbox,
    latest_verdicts,
    reviewers,
)


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_schema(c)
    push_report(c, project="p", title="T", risk="low", body="x", id="r")
    yield c
    c.close()


def _verdict(conn, reviewer_id, verdict, version=1, name=None):
    return add_verdict(
        conn,
        report_id="r",
        version=version,
        reviewer_id=reviewer_id,
        reviewer_name=name or reviewer_id.title(),
        verdict=verdict,
    )


def _comment(conn, author_id, name=None, parent_id=None, blocking=True):
    return add_comment(
        conn,
        report_id="r",
        version=1,
        author_id=author_id,
        author_name=name or author_id.title(),
        body="hi",
        parent_id=parent_id,
        blocking=blocking,
    )


def _verdicts(conn):
    return conn.execute("SELECT * FROM verdicts ORDER BY id").fetchall()


def test_status_awaiting_when_no_verdicts():
    assert compute_status([], 1, signed=True) == "awaiting_review"


def test_status_approved_single_approve(conn):
    _verdict(conn, "alice", "approve")
    assert compute_status(_verdicts(conn), 1, signed=True) == "approved"


def test_request_changes_wins_over_approve(conn):
    _verdict(conn, "alice", "approve")
    _verdict(conn, "bob", "request_changes")
    assert compute_status(_verdicts(conn), 1, signed=True) == "changes_requested"


def test_newer_verdict_by_same_reviewer_replaces_older(conn):
    _verdict(conn, "alice", "request_changes")
    _verdict(conn, "alice", "approve")
    rows = _verdicts(conn)
    assert latest_verdicts(rows, 1)["alice"]["verdict"] == "approve"
    assert compute_status(rows, 1, signed=True) == "approved"


def test_old_version_verdicts_do_not_count(conn):
    _verdict(conn, "alice", "approve", version=1)
    assert compute_status(_verdicts(conn), 2, signed=True) == "awaiting_review"


def test_reviewers_label_and_agent_excluded(conn):
    _comment(conn, "agent")
    _comment(conn, "alice")
    _verdict(conn, "bob", "approve", version=1)
    result = reviewers(
        conn.execute("SELECT * FROM comments ORDER BY id").fetchall(), _verdicts(conn)
    )
    assert [(r.id, r.name, r.verdict, r.verdict_version, r.label) for r in result] == [
        ("alice", "Alice", None, None, "reviewing"),
        ("bob", "Bob", "approve", 1, "approved v1"),
    ]


def test_reviewers_use_latest_name_and_verdict(conn):
    _comment(conn, "alice", name="Al")
    _verdict(conn, "alice", "request_changes", version=1, name="Alice")
    _verdict(conn, "alice", "approve", version=2, name="Alice B")
    (r,) = reviewers([], _verdicts(conn))
    assert (r.name, r.verdict, r.verdict_version, r.label) == (
        "Alice B",
        "approve",
        2,
        "approved v2",
    )


def test_reviewers_requested_changes_label(conn):
    _verdict(conn, "alice", "request_changes", version=1)
    (r,) = reviewers([], _verdicts(conn))
    assert r.label == "requested changes v1"


def test_reviewer_carries_latest_verdict_summary(conn):
    add_verdict(
        conn,
        report_id="r",
        version=1,
        reviewer_id="alice",
        reviewer_name="Alice",
        verdict="approve",
        summary="Fine.",
    )
    (r,) = reviewers([], _verdicts(conn))
    assert r.summary == "Fine."


def test_latest_verdict_is_by_id_not_created_at(conn):
    _verdict(conn, "alice", "request_changes")
    _verdict(conn, "alice", "approve")
    conn.execute("UPDATE verdicts SET created_at = '2000-01-01' WHERE verdict = 'approve'")
    assert compute_status(_verdicts(conn), 1, signed=True) == "approved"
    assert reviewers([], _verdicts(conn))[0].verdict == "approve"


def _threads(conn):
    comments = conn.execute("SELECT * FROM comments ORDER BY id").fetchall()
    return build_threads(comments, [], 1)


def test_can_request_changes_needs_summary_or_own_open_thread(conn):
    assert not can_request_changes(_threads(conn), "alice", "")
    assert not can_request_changes(_threads(conn), "alice", "   ")
    assert can_request_changes(_threads(conn), "alice", "needs work")

    root = _comment(conn, "alice")
    assert can_request_changes(_threads(conn), "alice", "")
    assert not can_request_changes(_threads(conn), "bob", "")

    resolve_thread(conn, "r", root["id"])
    assert not can_request_changes(_threads(conn), "alice", "")


def test_unsigned_overrides_verdicts(conn):
    assert compute_status(_verdicts(conn), 1, signed=False) == "unsigned"
    assert compute_status([], 1, signed=False) == "unsigned"
    assert compute_status([], 1, signed=True) == "awaiting_review"


def test_signed_is_required():
    with pytest.raises(TypeError):
        compute_status([], 1)


def test_can_request_changes_ignores_own_non_blocking_thread(conn):
    _comment(conn, "alice", blocking=False)
    assert not can_request_changes(_threads(conn), "alice", "")


def test_can_approve_needs_no_open_blocking_thread(conn):
    assert can_approve(_threads(conn))
    _comment(conn, "bob", blocking=False)
    assert can_approve(_threads(conn))
    root = _comment(conn, "bob")
    assert not can_approve(_threads(conn))
    resolve_thread(conn, "r", root["id"])
    assert can_approve(_threads(conn))
    _comment(conn, "alice", parent_id=root["id"])
    assert not can_approve(_threads(conn))


@pytest.mark.parametrize(
    ("shared", "user_id", "role", "root_author_id", "allowed"),
    [
        (False, "anyone00", None, "alice@example.com", True),
        (False, "anyone00", None, "agent", True),
        (True, "alice@example.com", "member", "alice@example.com", True),
        (True, "carol@example.com", "member", "alice@example.com", False),
        (True, "admin@example.com", "admin", "alice@example.com", True),
        (True, "admin@example.com", "admin", "agent", True),
        (True, "owner@example.com", "member", "agent", True),
        (True, "carol@example.com", "member", "agent", False),
        (True, "owner@example.com", "member", "alice@example.com", False),
    ],
)
def test_can_moderate(shared, user_id, role, root_author_id, allowed):
    assert (
        can_moderate(
            shared=shared,
            user_id=user_id,
            role=role,
            root_author_id=root_author_id,
            report_author_id="owner@example.com",
        )
        is allowed
    )


def test_can_moderate_agent_thread_without_report_owner():
    assert not can_moderate(
        shared=True,
        user_id="carol@example.com",
        role="member",
        root_author_id="agent",
        report_author_id=None,
    )


def row(
    id,
    status,
    risk="low",
    signed_at=None,
    updated_at="2026-10-01T00:00:00+00:00",
    reviewed_by=(),
    signer_id="author",
):
    return {
        "report": {"id": id, "risk": risk, "updated_at": updated_at, "signer_id": signer_id},
        "status": status,
        "signed_at": signed_at,
        "reviewed_by": set(reviewed_by),
    }


def test_inbox_groups_and_order():
    rows = [
        row("low-new", "awaiting_review", signed_at="2026-10-03T00:00:00+00:00"),
        row("high-new", "awaiting_review", "high", signed_at="2026-10-04T00:00:00+00:00"),
        row("low-old", "awaiting_review", signed_at="2026-10-02T00:00:00+00:00"),
        row("seen", "awaiting_review", signed_at="2026-10-01T00:00:00+00:00", reviewed_by=["me"]),
        row("self", "awaiting_review", signed_at="2026-10-01T00:00:00+00:00"),
        row("not-asked", "awaiting_review", signed_at="2026-10-01T00:00:00+00:00"),
        row("i-signed", "awaiting_review", signed_at="2026-10-01T00:00:00+00:00", signer_id="me"),
        row("returned", "changes_requested"),
        row("draft-old", "unsigned", updated_at="2026-10-01T00:00:00+00:00"),
        row("draft-new", "unsigned", updated_at="2026-10-05T00:00:00+00:00"),
        row("approved", "approved"),
    ]
    mine = {"self", "returned", "draft-old", "draft-new", "approved"}
    requested = {"low-new", "high-new", "low-old", "seen", "self", "i-signed"}
    groups = inbox(rows, user_id="me", mine=mine, requested=requested)

    def ids(key):
        return [r["report"]["id"] for r in groups[key]]

    assert ids("review") == ["high-new", "low-old", "low-new"]
    assert ids("sign") == ["draft-new", "draft-old"]
    assert ids("returned") == ["returned"]
    assert ids("waiting") == ["self"]
    assert inbox([], user_id="me", mine=set(), requested=set()) == {
        "review": [],
        "sign": [],
        "returned": [],
        "waiting": [],
    }
