import pytest

from verifair.db import add_comment, add_verdict, connect, init_schema, push_report, resolve_thread
from verifair.render import build_threads
from verifair.rules import can_request_changes, compute_status, latest_verdicts, reviewers


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


def _comment(conn, author_id, name=None, parent_id=None):
    return add_comment(
        conn,
        report_id="r",
        version=1,
        author_id=author_id,
        author_name=name or author_id.title(),
        body="hi",
        parent_id=parent_id,
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
