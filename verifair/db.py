import hashlib
import hmac
import itertools
import json
import logging
import re
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from verifair.render import slugify
from verifair.traces import (
    StoredTrace,
    TraceBinding,
    TraceDocument,
    canonical_json,
    validate_document,
)

log = logging.getLogger("verifair")

Target = Literal["implementation", "plan"]
Workflow = Literal["artifact", "plan_first"]
Row = dict[str, Any]
Connection = psycopg.Connection[Row]
CONNECT_KWARGS: dict[str, Any] = {"autocommit": True, "row_factory": dict_row}
SCHEMA_LOCK = 7_265_100_001
USERS_LOCK = 7_265_100_002

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    title TEXT NOT NULL,
    risk TEXT NOT NULL CHECK (risk IN ('low', 'medium', 'high')),
    ticket_url TEXT,
    links TEXT NOT NULL DEFAULT '[]',
    author TEXT NOT NULL,
    author_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    agent TEXT,
    model TEXT,
    session_url TEXT,
    seq BIGINT GENERATED ALWAYS AS IDENTITY
);
CREATE TABLE IF NOT EXISTS versions (
    report_id TEXT NOT NULL REFERENCES reports(id),
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    plan TEXT,
    created_at TEXT NOT NULL,
    "commit" TEXT,
    PRIMARY KEY (report_id, version)
);
CREATE TABLE IF NOT EXISTS comments (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports(id),
    version INTEGER NOT NULL,
    parent_id BIGINT REFERENCES comments(id),
    section TEXT,
    quote TEXT,
    author_id TEXT NOT NULL,
    author_name TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    blocking INTEGER NOT NULL DEFAULT 1,
    prefix TEXT,
    suffix TEXT,
    resolved_by TEXT
);
CREATE TABLE IF NOT EXISTS verdicts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports(id),
    version INTEGER NOT NULL,
    reviewer_id TEXT NOT NULL,
    reviewer_name TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('approve', 'request_changes')),
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signatures (
    report_id TEXT NOT NULL REFERENCES reports(id),
    version INTEGER NOT NULL,
    signer_id TEXT NOT NULL,
    signer_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (report_id, version)
);
CREATE TABLE IF NOT EXISTS review_requests (
    report_id TEXT NOT NULL REFERENCES reports(id),
    reviewer_id TEXT NOT NULL,
    PRIMARY KEY (report_id, reviewer_id)
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    owner_id TEXT NOT NULL REFERENCES users(id),
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    nonce TEXT,
    last_used_at TEXT,
    seq BIGINT GENERATED ALWAYS AS IDENTITY
);
CREATE INDEX IF NOT EXISTS comments_report_parent ON comments(report_id, parent_id);
CREATE INDEX IF NOT EXISTS verdicts_report ON verdicts(report_id);
CREATE INDEX IF NOT EXISTS reports_project ON reports(project);
CREATE INDEX IF NOT EXISTS review_requests_reviewer ON review_requests(reviewer_id);
CREATE INDEX IF NOT EXISTS api_tokens_owner ON api_tokens(owner_id);
CREATE UNIQUE INDEX IF NOT EXISTS api_tokens_owner_nonce ON api_tokens(owner_id, nonce)
    WHERE nonce IS NOT NULL;
CREATE INDEX IF NOT EXISTS reports_author ON reports(author_id);
"""


class ProjectMismatch(Exception):
    pass


class WorkflowMismatch(Exception):
    pass


class PlanNotApproved(Exception):
    def __init__(self, plan_revision: int) -> None:
        super().__init__("current plan is not approved")
        self.plan_revision = plan_revision


class MissingDocument(Exception):
    def __init__(self, target: Target) -> None:
        super().__init__(f"{target} has not been attached")
        self.target = target


class StaleRevision(Exception):
    def __init__(self, target: Target, current_revision: int) -> None:
        super().__init__("document changed")
        self.target = target
        self.current_revision = current_revision


class AlreadySigned(Exception):
    pass


class VerdictRefused(Exception):
    pass


class AlreadyCreated(Exception):
    pass


class LastAdmin(Exception):
    pass


class InactiveMember(Exception):
    pass


class NotOwner(Exception):
    pass


class NoOwner(Exception):
    pass


class StaleVersion(Exception):
    def __init__(self, latest: int) -> None:
        super().__init__(f"latest version is {latest}")
        self.latest = latest


def _short(user_id: str) -> str:
    return f"{user_id[:4]}…"


# Postgres text cannot hold NUL, and UTF-8 cannot encode a lone surrogate (JSON "\ud800")
UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def storable(text: str) -> bool:
    return UNSTORABLE.search(text) is None


def no_nul(text: str | None) -> str | None:
    if text is not None and not storable(text):
        raise ValueError("must not contain NUL characters or lone surrogates")
    return text


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _now_exact() -> str:
    return datetime.now(UTC).isoformat()


def connect(url: str) -> Connection:
    return psycopg.connect(url, **CONNECT_KWARGS)


def open_pool(url: str, max_size: int) -> ConnectionPool:
    # one dead connection usually means the server restarted and every idle one is dead.
    # Dropping them all at once spares getconn() trying each with backoff (0, 1, 2, 4 s).
    # The pool calls this outside its lock, and check() tests with the static
    # check_connection, so this neither deadlocks nor recurses.
    def check(conn: Connection) -> None:
        try:
            ConnectionPool.check_connection(conn)
        except Exception:
            pool.check()
            raise

    pool = ConnectionPool(
        url,
        min_size=1,
        max_size=max_size,
        timeout=10,
        kwargs=CONNECT_KWARGS,
        check=check,
        open=False,
    )
    pool.open(wait=True, timeout=10)
    return pool


def _lock_report(conn: Connection, id: str) -> Row | None:
    return conn.execute("SELECT * FROM reports WHERE id=%s FOR UPDATE", (id,)).fetchone()


@contextmanager
def _write(conn: Connection, report_id: str | None = None) -> Iterator[None]:
    # writes on one report hold its row lock, so version checks, validate() and the insert
    # see no concurrent change to that report until commit. It also makes a report's comment
    # and verdict ids commit in id order, which the /wait id cursor relies on.
    with conn.transaction():
        if report_id is not None:
            _lock_report(conn, report_id)
        yield


def _check_version(conn: Connection, report_id: str, expected: int | None) -> None:
    if expected is not None and (latest := latest_version(conn, report_id)) != expected:
        raise StaleVersion(latest)


def init_schema(conn: Connection) -> None:
    with conn.transaction():
        # DDL waiting behind a long transaction would block all traffic on that table
        conn.execute("SET LOCAL lock_timeout = '5s'")
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK,))
        conn.execute(SCHEMA)
        # ALTER takes an ACCESS EXCLUSIVE lock even when the column exists, so only run it once
        if not conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()"
            " AND table_name = 'review_requests' AND column_name = 'requested_by'"
        ).fetchone():
            conn.execute("ALTER TABLE review_requests ADD COLUMN IF NOT EXISTS requested_by TEXT")
        if not conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()"
            " AND table_name = 'versions' AND column_name = 'plan'"
        ).fetchone():
            conn.execute("ALTER TABLE versions ADD COLUMN IF NOT EXISTS plan TEXT")

        columns = {
            (row["table_name"], row["column_name"])
            for row in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns"
                " WHERE table_schema=current_schema()"
            )
        }
        if ("reports", "workflow") not in columns:
            conn.execute(
                "ALTER TABLE reports ADD COLUMN workflow TEXT NOT NULL DEFAULT 'artifact'"
                " CHECK (workflow IN ('artifact', 'plan_first'))"
            )
        for table in ("comments", "verdicts", "signatures"):
            if (table, "target") not in columns:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN target TEXT NOT NULL DEFAULT 'implementation'"
                    " CHECK (target IN ('implementation', 'plan'))"
                )
                if table == "signatures":
                    conn.execute(
                        "ALTER TABLE signatures DROP CONSTRAINT signatures_pkey,"
                        " ADD PRIMARY KEY (report_id, target, version)"
                    )
        if ("versions", "plan_revision") not in columns:
            conn.execute("ALTER TABLE versions ADD COLUMN plan_revision INTEGER")
            conn.execute(
                "WITH previous AS ("
                " SELECT report_id, version, plan,"
                " lag(plan) OVER (PARTITION BY report_id ORDER BY version) AS previous_plan"
                " FROM versions), sources AS ("
                " SELECT report_id, version, plan, max(CASE WHEN"
                ' plan COLLATE "C" IS DISTINCT FROM previous_plan COLLATE "C" THEN version END)'
                " OVER (PARTITION BY report_id ORDER BY version) AS source FROM previous)"
                " UPDATE versions v SET plan_revision = CASE WHEN s.plan IS NOT NULL"
                " THEN s.source END FROM sources s"
                " WHERE v.report_id=s.report_id AND v.version=s.version"
            )
            conn.execute(
                "ALTER TABLE versions ALTER COLUMN body DROP NOT NULL,"
                " ADD CONSTRAINT versions_content CHECK ("
                " (body IS NOT NULL OR plan IS NOT NULL) AND"
                " (body IS NULL OR body <> '') AND (plan IS NULL OR plan <> '')),"
                " ADD CONSTRAINT versions_plan_pointer CHECK ("
                " (plan IS NULL) = (plan_revision IS NULL) AND"
                " (plan_revision IS NULL OR (plan_revision > 0 AND plan_revision <= version))),"
                " ADD CONSTRAINT versions_plan_source FOREIGN KEY (report_id, plan_revision)"
                " REFERENCES versions(report_id, version)"
            )
        if ("versions", "trace") not in columns:
            conn.execute(
                "ALTER TABLE versions ADD COLUMN trace TEXT,"
                " ADD CONSTRAINT versions_trace_body CHECK (trace IS NULL OR body IS NOT NULL)"
            )
        for column in ("oidc_sub", "sessions_valid_after"):
            if ("users", column) not in columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")


def target_document(
    conn: Connection, report_id: str, snapshot: int, target: Target
) -> tuple[int, str] | None:
    if target not in ("implementation", "plan"):
        raise ValueError("invalid target")
    row = get_version(conn, report_id, snapshot)
    if row is None:
        return None
    document = row["body" if target == "implementation" else "plan"]
    if document is None:
        return None
    return (snapshot if target == "implementation" else row["plan_revision"], document)


def _check_target(
    conn: Connection,
    report_id: str,
    target: Target,
    revision: int,
    expected_version: int | None,
) -> None:
    _check_version(conn, report_id, expected_version)
    report = get_report(conn, report_id)
    if report is None:
        raise LookupError(f"report {report_id!r} not found")
    if target == "plan" and report["workflow"] != "plan_first":
        raise ValueError("artifact plans are read-only")
    document = target_document(conn, report_id, latest_version(conn, report_id), target)
    if document is None:
        raise MissingDocument(target)
    if revision != document[0]:
        raise StaleRevision(target, document[0])


def plan_review_state(conn: Connection, report_id: str, snapshot: int | None = None) -> dict | None:
    from verifair.rules import compute_status, latest_verdicts, plan_status

    report = get_report(conn, report_id)
    if report is None or report["workflow"] != "plan_first":
        return None
    snapshot = latest_version(conn, report_id) if snapshot is None else snapshot
    document = target_document(conn, report_id, snapshot, "plan")
    if document is None:
        return None
    revision, _ = document
    signature = get_signature(conn, report_id, revision, target="plan")
    verdicts = [
        row for row in list_verdicts(conn, report_id, target="plan") if row["version"] <= revision
    ]
    blockers = conn.execute(
        "SELECT count(*) AS n FROM comments WHERE report_id=%s AND target='plan'"
        " AND parent_id IS NULL AND resolved_at IS NULL AND blocking=1 AND version<=%s",
        (report_id, revision),
    ).fetchone()["n"]
    status = compute_status(
        verdicts,
        revision,
        signature is not None,
        signature["signer_id"] if signature else None,
    )
    return {
        "revision": revision,
        "signature": signature,
        "verdicts": verdicts,
        "latest_verdicts": latest_verdicts(verdicts, revision),
        "status": plan_status(status, blockers),
        "open_blocking": blockers,
    }


def _require_plan_approved(conn: Connection, report_id: str) -> None:
    state = plan_review_state(conn, report_id)
    if state is not None and state["status"] != "approved":
        raise PlanNotApproved(state["revision"])


def push_report(
    conn: Connection,
    *,
    project: str,
    title: str,
    risk: str,
    body: str | None,
    body_supplied: bool = True,
    workflow: Workflow | None = None,
    expected_version: int | None = None,
    plan: str | None = None,
    inherit_plan: bool = True,
    id: str | None = None,
    ticket_url: str | None = None,
    links: list[str] | None = None,
    author: str | None = None,
    author_id: str | None = None,
    reviewers: list[str] | None = None,
    agent: str | None = None,
    model: str | None = None,
    session_url: str | None = None,
    commit: str | None = None,
    owner_id: str | None = None,
    trace: TraceDocument | None = None,
    trace_supplied: bool = False,
) -> tuple[str, int]:
    if workflow not in (None, "artifact", "plan_first"):
        raise ValueError("invalid workflow")
    if not body_supplied and body is not None:
        raise ValueError("body must be omitted when body_supplied is false")
    for name, document in (("body", body), ("plan", plan)):
        if document is not None and (not document.strip() or not storable(document)):
            raise ValueError(f"{name} must be nonblank storable Markdown")
    if trace is not None:
        if not body_supplied or body is None:
            raise ValueError("trace requires supplied body")
        trace = validate_document(trace)
    now = _now()
    links_json = json.dumps(links or [])
    if owner_id is not None:
        author_id = owner_id
    with _write(conn):
        existing = _lock_report(conn, id) if id else None
        if existing is None:
            base = slugify(title)
            candidates = (
                [id] if id else (f"{base}-{n}" if n > 1 else base for n in itertools.count(1))
            )
            for candidate in candidates:
                inserted = conn.execute(
                    "INSERT INTO reports (id, project, title, risk, ticket_url, links,"
                    " author, author_id,"
                    " created_at, updated_at, agent, model, session_url)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT (id) DO NOTHING RETURNING id",
                    (
                        candidate,
                        project,
                        title,
                        risk,
                        ticket_url,
                        links_json,
                        author or "agent",
                        author_id,
                        now,
                        now,
                        agent,
                        model,
                        session_url,
                    ),
                ).fetchone()
                if inserted is not None:
                    id, version = candidate, 1
                    break
            else:
                existing = _lock_report(conn, id)
        if existing is not None:
            if owner_id is not None and existing["author_id"] is None:
                raise NoOwner(f"report {id!r} has no owner")
            if owner_id is not None and existing["author_id"] != owner_id:
                raise NotOwner(f"report {id!r} belongs to another member")
            if existing["project"] != project:
                log.info(
                    "push rejected id=%s project=%s owner=%s", id, project, existing["project"]
                )
                raise ProjectMismatch(f"report {id!r} belongs to project {existing['project']!r}")
            _check_version(conn, id, expected_version)
            if workflow is not None and workflow != existing["workflow"]:
                raise WorkflowMismatch("workflow cannot be changed")
            workflow = existing["workflow"]
            if workflow == "plan_first" and expected_version is None:
                raise ValueError("expected_version is required for plan-first updates")
            if trace is not None and expected_version is None:
                raise ValueError("expected_version is required for trace updates")
            version = latest_version(conn, id) + 1
            conn.execute(
                "UPDATE reports SET title=%s, risk=%s, ticket_url=%s, links=%s,"
                " author=COALESCE(%s, author), author_id=COALESCE(%s, author_id), updated_at=%s,"
                " agent=%s, model=%s, session_url=%s WHERE id=%s",
                (
                    title,
                    risk,
                    ticket_url,
                    links_json,
                    author,
                    author_id,
                    now,
                    agent,
                    model,
                    session_url,
                    id,
                ),
            )
        else:
            if expected_version is not None:
                raise ValueError("expected_version must be omitted on creation")
            workflow = workflow or "artifact"
            conn.execute("UPDATE reports SET workflow=%s WHERE id=%s", (workflow, id))
        plan_supplied = plan is not None or not inherit_plan
        previous = get_version(conn, id, version - 1) if version > 1 else None
        if previous is not None and trace_supplied and not body_supplied:
            raise ValueError("trace requires supplied body")
        if plan is None and inherit_plan and previous is not None:
            plan = previous["plan"]
        plan_revision = (
            None
            if plan is None
            else (previous["plan_revision"] if previous and previous["plan"] == plan else version)
        )
        if workflow == "artifact":
            if not body_supplied or body is None:
                raise ValueError("artifact reviews require body")
        else:
            if plan is None:
                raise ValueError("plan-first reviews require plan")
            if previous is None:
                if body is not None:
                    raise ValueError("initial plan-first review cannot contain body")
            else:
                if not body_supplied and not plan_supplied:
                    raise ValueError("at least one content field must be supplied")
                if body_supplied and body is None:
                    raise ValueError("implementation cannot be removed")
                if body_supplied:
                    if plan_revision != previous["plan_revision"]:
                        raise PlanNotApproved(plan_revision)
                    _require_plan_approved(conn, id)
                else:
                    body = previous["body"]
            if not body_supplied:
                if commit is not None:
                    raise ValueError("commit requires supplied body")
                commit = previous["commit"] if previous else None
            elif body is None and commit is not None:
                raise ValueError("commit requires supplied body")
        stored_trace = None
        if trace is not None:
            stored_trace = canonical_json(
                StoredTrace(
                    binding=TraceBinding(
                        version=version, plan_revision=plan_revision, commit=commit
                    ),
                    document=trace,
                )
            )
        elif not body_supplied and previous is not None:
            stored_trace = previous["trace"]
        owner = author_id if author_id is not None else (existing or {}).get("author_id")
        if reviewers:
            conn.cursor().executemany(
                "INSERT INTO review_requests (report_id, reviewer_id, requested_by)"
                " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                [(id, reviewer, owner_id) for reviewer in set(reviewers) - {owner}],
            )
        conn.execute(
            "INSERT INTO versions (report_id, version, body, plan, plan_revision, created_at,"
            ' "commit", trace)'
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (id, version, body, plan, plan_revision, now, commit, stored_trace),
        )
    log.info("report pushed id=%s project=%s version=%d", id, project, version)
    return id, version


def get_report(conn: Connection, id: str) -> Row | None:
    return conn.execute("SELECT * FROM reports WHERE id=%s", (id,)).fetchone()


def get_version(conn: Connection, id: str, version: int) -> Row | None:
    return conn.execute(
        "SELECT * FROM versions WHERE report_id=%s AND version=%s", (id, version)
    ).fetchone()


def get_trace(conn: Connection, report_id: str, version: int) -> StoredTrace | None:
    row = get_version(conn, report_id, version)
    if row is None or row["trace"] is None:
        return None
    return StoredTrace.model_validate_json(row["trace"])


def latest_version(conn: Connection, id: str) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM versions WHERE report_id=%s", (id,)
    ).fetchone()
    return row["v"] or 0


def list_versions(conn: Connection, id: str) -> list[Row]:
    return conn.execute(
        'SELECT report_id, version, body, plan, plan_revision, created_at, "commit",'
        " trace IS NOT NULL AS has_trace FROM versions WHERE report_id=%s ORDER BY version",
        (id,),
    ).fetchall()


def project_exists(conn: Connection, project: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM reports WHERE project=%s LIMIT 1", (project,)).fetchone()
        is not None
    )


def list_reports(conn: Connection) -> list[Row]:
    return conn.execute(
        "SELECT r.*, v.version AS latest_version, v.plan_revision, v.body IS NOT NULL AS has_body,"
        " s.signer_id, s.created_at AS signed_at,"
        " s.signer_id AS implementation_signer_id, s.signer_name AS implementation_signer_name,"
        " s.created_at AS implementation_signed_at,"
        " ps.signer_id AS plan_signer_id, ps.signer_name AS plan_signer_name,"
        " ps.created_at AS plan_signed_at,"
        " COALESCE(c.open_threads, 0) AS open_threads,"
        " COALESCE(c.open_plan_threads, 0) AS open_plan_threads,"
        " COALESCE(c.open_plan_blocking, 0) AS open_plan_blocking"
        " FROM reports r JOIN LATERAL (SELECT * FROM versions WHERE report_id=r.id"
        " ORDER BY version DESC LIMIT 1) v ON TRUE"
        " LEFT JOIN signatures s ON s.report_id=r.id AND s.version=v.version"
        " AND s.target='implementation'"
        " LEFT JOIN signatures ps ON ps.report_id=r.id AND ps.version=v.plan_revision"
        " AND ps.target='plan'"
        " LEFT JOIN (SELECT report_id,"
        " count(*) FILTER (WHERE target='implementation') AS open_threads,"
        " count(*) FILTER (WHERE target='plan') AS open_plan_threads,"
        " count(*) FILTER (WHERE target='plan' AND blocking=1) AS open_plan_blocking"
        " FROM comments WHERE parent_id IS NULL AND resolved_at IS NULL GROUP BY report_id) c"
        " ON c.report_id=r.id ORDER BY r.created_at DESC, r.seq DESC"
    ).fetchall()


def _by_report(conn: Connection, sql: str, ids: list[str]) -> dict[str, list[Row]]:
    grouped: dict[str, list[Row]] = {id: [] for id in ids}
    for row in conn.execute(sql):
        if row["report_id"] in grouped:
            grouped[row["report_id"]].append(row)
    return grouped


def verdicts_by_report(conn: Connection, ids: list[str]) -> dict[str, list[Row]]:
    return _by_report(
        conn,
        "SELECT id, report_id, target, version, reviewer_id, reviewer_name, verdict, summary,"
        " created_at"
        " FROM verdicts ORDER BY id",
        ids,
    )


def requests_by_report(conn: Connection, ids: list[str]) -> dict[str, list[str]]:
    rows = _by_report(
        conn,
        'SELECT report_id, reviewer_id FROM review_requests ORDER BY reviewer_id COLLATE "C"',
        ids,
    )
    return {id: [r["reviewer_id"] for r in group] for id, group in rows.items()}


def requested_reviewers(conn: Connection, report_id: str) -> list[str]:
    return [
        row["reviewer_id"]
        for row in conn.execute(
            "SELECT reviewer_id FROM review_requests WHERE report_id=%s"
            ' ORDER BY reviewer_id COLLATE "C"',
            (report_id,),
        )
    ]


def review_requesters(conn: Connection, report_id: str) -> dict[str, str | None]:
    rows = conn.execute(
        "SELECT reviewer_id, requested_by FROM review_requests WHERE report_id=%s", (report_id,)
    )
    return {row["reviewer_id"]: row["requested_by"] for row in rows}


def add_review_request(
    conn: Connection, report_id: str, reviewer_id: str, requested_by: str | None = None
) -> bool:
    return (
        conn.execute(
            "INSERT INTO review_requests (report_id, reviewer_id, requested_by)"
            " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (report_id, reviewer_id, requested_by),
        ).rowcount
        > 0
    )


def remove_review_request(conn: Connection, report_id: str, reviewer_id: str) -> str:
    with _write(conn, report_id):
        judged = conn.execute(
            "SELECT 1 FROM verdicts d JOIN versions v ON v.report_id=d.report_id"
            " WHERE d.report_id=%s AND d.reviewer_id=%s"
            " AND v.version=(SELECT MAX(version) FROM versions WHERE report_id=%s)"
            " AND ((d.target='implementation' AND v.body IS NOT NULL AND d.version=v.version)"
            " OR (d.target='plan' AND d.version=v.plan_revision)) LIMIT 1",
            (report_id, reviewer_id, report_id),
        ).fetchone()
        if judged:
            return "kept"
        removed = conn.execute(
            "DELETE FROM review_requests WHERE report_id=%s AND reviewer_id=%s",
            (report_id, reviewer_id),
        ).rowcount
    return "removed" if removed else "missing"


def add_comment(
    conn: Connection,
    *,
    report_id: str,
    version: int | None = None,
    target: Target | None = None,
    validate: Callable[[str, int], str | None] | None = None,
    author_id: str,
    author_name: str,
    body: str,
    parent_id: int | None = None,
    section: str | None = None,
    quote: str | None = None,
    expected_version: int | None = None,
    blocking: bool = True,
    prefix: str | None = None,
    suffix: str | None = None,
    reopen: bool = True,
) -> Row:
    with _write(conn, report_id):
        _check_version(conn, report_id, expected_version)
        root = None
        if parent_id is not None:
            root = thread_root(conn, report_id, parent_id)
            if root is None:
                raise ValueError(f"parent comment {parent_id} not found on report {report_id!r}")
            if target is not None and target != root["target"]:
                raise ValueError("reply target conflicts with root target")
            target = root["target"]
        target = "implementation" if target is None else target
        document = target_document(conn, report_id, latest_version(conn, report_id), target)
        checked_revision = version if version is not None else (document[0] if document else 0)
        _check_target(conn, report_id, target, checked_revision, expected_version)
        if document is None:
            raise MissingDocument(target)
        revision, markdown = document
        if version is None and target == "plan" and root is None:
            raise ValueError("plan revision is required")
        if validate is not None and (error := validate(markdown, revision)):
            raise ValueError(error)
        version = revision
        if root is not None:
            parent_id = root["id"]
            version, section, quote = root["version"], root["section"], root["quote"]
            prefix, suffix = root["prefix"], root["suffix"]
            blocking = bool(root["blocking"])
            if reopen:
                reopen_thread(conn, report_id, parent_id)
        row = conn.execute(
            "INSERT INTO comments (report_id, version, parent_id, section, quote, author_id,"
            " author_name, body, created_at, blocking, prefix, suffix, target)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (
                report_id,
                version,
                parent_id,
                section,
                quote,
                author_id,
                author_name,
                body,
                _now(),
                int(blocking),
                prefix,
                suffix,
                target,
            ),
        ).fetchone()
    log.info(
        "comment added id=%d report=%s version=%d parent=%s author_id=%s",
        row["id"],
        report_id,
        version,
        parent_id,
        _short(author_id),
    )
    return row


def list_comments(
    conn: Connection, report_id: str, open_only: bool = False, *, target: Target = "implementation"
) -> list[Row]:
    sql = (
        "SELECT c.* FROM comments c JOIN comments root ON root.id = COALESCE(c.parent_id, c.id)"
        " WHERE c.report_id=%s AND c.target=%s"
    )
    if open_only:
        sql += " AND root.resolved_at IS NULL"
    return conn.execute(sql + " ORDER BY c.id", (report_id, target)).fetchall()


def resolve_thread(
    conn: Connection, report_id: str, comment_id: int, *, by: str | None = None
) -> int | None:
    with _write(conn, report_id):
        row = conn.execute(
            "SELECT COALESCE(parent_id, id) AS root_id FROM comments WHERE id=%s AND report_id=%s",
            (comment_id, report_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE comments SET resolved_at=%s, resolved_by=%s"
            " WHERE id=%s AND resolved_at IS NULL",
            (_now(), by, row["root_id"]),
        )
    return row["root_id"]


def thread_root(conn: Connection, report_id: str, comment_id: int) -> Row | None:
    return conn.execute(
        "SELECT root.* FROM comments c JOIN comments root ON root.id = COALESCE(c.parent_id, c.id)"
        " WHERE c.id=%s AND c.report_id=%s",
        (comment_id, report_id),
    ).fetchone()


def reopen_thread(conn: Connection, report_id: str, comment_id: int) -> bool:
    with _write(conn, report_id):
        cur = conn.execute(
            "UPDATE comments SET resolved_at=NULL, resolved_by=NULL WHERE report_id=%s AND id ="
            " (SELECT COALESCE(parent_id, id) FROM comments WHERE id=%s AND report_id=%s)",
            (report_id, comment_id, report_id),
        )
    return cur.rowcount > 0


def add_verdict(
    conn: Connection,
    *,
    report_id: str,
    version: int,
    target: Target = "implementation",
    reviewer_id: str,
    reviewer_name: str,
    verdict: str,
    summary: str = "",
    expected_version: int | None = None,
    validate: Callable[[], str | None] | None = None,
) -> Row:
    with _write(conn, report_id):
        _check_target(conn, report_id, target, version, expected_version)
        if target == "implementation":
            _require_plan_approved(conn, report_id)
        if validate is not None and (error := validate()):
            raise VerdictRefused(error)
        row = conn.execute(
            "INSERT INTO verdicts (report_id, version, reviewer_id, reviewer_name, verdict,"
            " summary, created_at, target) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (report_id, version, reviewer_id, reviewer_name, verdict, summary, _now(), target),
        ).fetchone()
    log.info(
        "verdict added report=%s version=%d reviewer_id=%s verdict=%s",
        report_id,
        version,
        _short(reviewer_id),
        verdict,
    )
    return row


def list_verdicts(
    conn: Connection, report_id: str, *, target: Target = "implementation"
) -> list[Row]:
    return conn.execute(
        "SELECT * FROM verdicts WHERE report_id=%s AND target=%s ORDER BY id", (report_id, target)
    ).fetchall()


def set_blocking(
    conn: Connection,
    report_id: str,
    comment_id: int,
    blocking: bool,
    *,
    expected_version: int | None = None,
) -> bool:
    with _write(conn, report_id):
        _check_version(conn, report_id, expected_version)
        cur = conn.execute(
            "UPDATE comments SET blocking=%s WHERE id=%s AND report_id=%s AND parent_id IS NULL",
            (int(blocking), comment_id, report_id),
        )
    return cur.rowcount > 0


def sign_version(
    conn: Connection,
    *,
    report_id: str,
    version: int,
    signer_id: str,
    signer_name: str,
    target: Target = "implementation",
    expected_version: int | None = None,
) -> Row:
    with _write(conn, report_id):
        guard = (
            version if target == "implementation" and expected_version is None else expected_version
        )
        _check_target(conn, report_id, target, version, guard)
        if target == "implementation":
            _require_plan_approved(conn, report_id)
        signed = conn.execute(
            "INSERT INTO signatures (report_id, version, signer_id, signer_name, created_at,"
            " target)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (report_id, target, version) DO NOTHING RETURNING report_id",
            (report_id, version, signer_id, signer_name, _now(), target),
        ).fetchone()
        if signed is None:
            raise AlreadySigned(f"{report_id} v{version} is already signed")
        conn.execute(
            "UPDATE reports SET author_id=%s WHERE id=%s AND author_id IS NULL",
            (signer_id, report_id),
        )
    log.info(
        "version signed report=%s version=%d signer_id=%s", report_id, version, _short(signer_id)
    )
    return get_signature(conn, report_id, version, target=target)


def get_signature(
    conn: Connection, report_id: str, version: int, *, target: Target = "implementation"
) -> Row | None:
    return conn.execute(
        "SELECT * FROM signatures WHERE report_id=%s AND version=%s AND target=%s",
        (report_id, version, target),
    ).fetchone()


ROLES = ("admin", "member")
TOKEN_COLUMNS = "id, owner_id, label, created_at, revoked_at, last_used_at"
TOKEN_TOUCH_SECONDS = 60
TOKEN_TOUCH_WAIT_MS = 100


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")


def add_user(conn: Connection, *, id: str, name: str, role: str) -> Row:
    _check_role(role)
    id, now = id.lower(), _now()
    with _write(conn):
        added = conn.execute(
            "INSERT INTO users (id, name, role, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING RETURNING id",
            (id, name, role, now, now),
        ).fetchone()
        if added is None:
            raise ValueError(f"user {id!r} already exists")
    return get_user(conn, id)


def get_user(conn: Connection, id: str) -> Row | None:
    return conn.execute("SELECT * FROM users WHERE id=%s", (id,)).fetchone()


def list_users(conn: Connection) -> list[Row]:
    return conn.execute(
        'SELECT * FROM users ORDER BY role != \'admin\', name COLLATE "C", id COLLATE "C"'
    ).fetchall()


def update_user(
    conn: Connection, id: str, *, role: str | None = None, active: bool | None = None
) -> tuple[Row, int]:
    if role is not None:
        _check_role(role)
    now, revoked = _now(), 0
    with _write(conn):
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (USERS_LOCK,))
        user = get_user(conn, id)
        if user is None:
            raise ValueError(f"user {id!r} not found")
        active = bool(user["active"]) if active is None else active
        if not user["active"] and not active and role not in (None, user["role"]):
            raise InactiveMember("Reactivate this member before changing their role.")
        role = role or user["role"]
        if user["role"] == "admin" and user["active"] and (role != "admin" or not active):
            others = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND active=1 AND id!=%s", (id,)
            ).fetchone()["n"]
            if not others:
                raise LastAdmin("There must be at least one active admin.")
        conn.execute(
            "UPDATE users SET role=%s, active=%s, updated_at=%s WHERE id=%s",
            (role, int(active), now, id),
        )
        if not active:
            conn.execute("UPDATE users SET sessions_valid_after=%s WHERE id=%s", (_now_exact(), id))
            revoked = conn.execute(
                "UPDATE api_tokens SET revoked_at=%s WHERE owner_id=%s AND revoked_at IS NULL",
                (now, id),
            ).rowcount
    return get_user(conn, id), revoked


HUMAN_ID_COLUMNS = (
    ("reports", "author_id"),
    ("signatures", "signer_id"),
    ("verdicts", "reviewer_id"),
    ("comments", "author_id"),
    ("comments", "resolved_by"),
    ("review_requests", "reviewer_id"),
)


def lowercase_human_ids(conn: Connection) -> int:
    # local mode stores ids as sent, shared mode matches them to the lower-cased sign-in
    # email. Python's lower() is how the email is read, and Postgres lower() differs from
    # it outside ASCII, so Python maps the ids and one statement per column applies the map.
    # An old human id such as "Agent" stays as it is so it does not turn into the agent.
    changed = 0
    mapping = "unnest(%s::text[], %s::text[]) AS m(old, new)"
    with _write(conn):
        for table, column in HUMAN_ID_COLUMNS:
            rows = conn.execute(
                f"SELECT DISTINCT {column} AS id FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall()
            old = [r["id"] for r in rows if r["id"].lower() not in (r["id"], "agent")]
            if not old:
                continue
            params = (old, [id.lower() for id in old])
            if table == "review_requests":
                conn.execute(
                    "INSERT INTO review_requests (report_id, reviewer_id, requested_by)"
                    f" SELECT r.report_id, m.new, r.requested_by FROM review_requests r, {mapping}"
                    " WHERE r.reviewer_id = m.old ON CONFLICT DO NOTHING",
                    params,
                )
                sql = f"DELETE FROM review_requests r USING {mapping} WHERE r.reviewer_id = m.old"
            else:
                sql = (
                    f"UPDATE {table} t SET {column} = m.new FROM {mapping} WHERE t.{column} = m.old"
                )
            changed += conn.execute(sql, params).rowcount
    return changed


def link_account(conn: Connection, id: str, sub: str) -> bool | None:
    """Link a member to their provider account on first sign-in. None when not a member."""
    with _write(conn):
        row = conn.execute(
            "UPDATE users SET oidc_sub = COALESCE(oidc_sub, %s) WHERE id=%s RETURNING oidc_sub",
            (sub, id),
        ).fetchone()
    return None if row is None else row["oidc_sub"] == sub


def reset_sign_in(conn: Connection, id: str) -> bool:
    with _write(conn):
        return (
            conn.execute(
                "UPDATE users SET oidc_sub=NULL, sessions_valid_after=%s, updated_at=%s"
                " WHERE id=%s",
                (_now_exact(), _now(), id),
            ).rowcount
            > 0
        )


def bootstrap_admin(conn: Connection, admin_id: str) -> bool:
    admin_id, now = admin_id.lower(), _now()
    with _write(conn):
        cur = conn.execute(
            "INSERT INTO users (id, name, role, created_at, updated_at)"
            " SELECT %s, %s, 'admin', %s, %s WHERE NOT EXISTS (SELECT 1 FROM users)",
            (admin_id, (admin_id.partition("@")[0] or admin_id)[:60], now, now),
        )
    return cur.rowcount > 0


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def create_token(
    conn: Connection, *, owner_id: str, label: str, nonce: str | None = None
) -> tuple[str, Row]:
    public_id, secret = secrets.token_hex(6), secrets.token_urlsafe(32)
    with _write(conn):
        # FOR SHARE makes a concurrent deactivation wait, so it also revokes this token
        owner = conn.execute("SELECT * FROM users WHERE id=%s FOR SHARE", (owner_id,)).fetchone()
        if owner is None or not owner["active"]:
            raise ValueError("unknown or inactive owner")
        created = conn.execute(
            "INSERT INTO api_tokens (id, digest, owner_id, label, created_at, nonce)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (owner_id, nonce) WHERE nonce IS NOT NULL DO NOTHING RETURNING id",
            (public_id, _digest(secret), owner_id, label, _now(), nonce),
        ).fetchone()
        if created is None:
            raise AlreadyCreated
    return f"vf_{public_id}.{secret}", get_token(conn, public_id)


def get_token(conn: Connection, token_id: str) -> Row | None:
    return conn.execute(
        f"SELECT {TOKEN_COLUMNS} FROM api_tokens WHERE id=%s", (token_id,)
    ).fetchone()


def _touch_token(conn: Connection, token_id: str, cutoff: str) -> None:
    # the stamp is a nice-to-have, so an API call never waits long on another writer for it
    with conn.transaction():
        conn.execute(f"SET LOCAL lock_timeout = '{TOKEN_TOUCH_WAIT_MS}ms'")
        conn.execute(
            "UPDATE api_tokens SET last_used_at=%s"
            " WHERE id=%s AND (last_used_at IS NULL OR last_used_at < %s)",
            (_now(), token_id, cutoff),
        )


def authenticate_token(
    conn: Connection, token: str, *, touch: bool = False
) -> tuple[Row | None, str | None]:
    if not token.startswith("vf_") or token.count(".") != 1:
        return None, "malformed"
    public_id, secret = token[3:].split(".")
    if "\x00" in public_id:  # Postgres text cannot hold NUL, so no stored id can match
        return None, "unknown"
    row = conn.execute(
        "SELECT digest, owner_id, revoked_at, last_used_at FROM api_tokens WHERE id=%s",
        (public_id,),
    ).fetchone()
    if row is None or not hmac.compare_digest(row["digest"], _digest(secret)):
        return None, "unknown"
    if row["revoked_at"] is not None:
        return None, "revoked"
    owner = get_user(conn, row["owner_id"])
    if owner is None or not owner["active"]:
        return None, "inactive"
    cutoff = (datetime.now(UTC) - timedelta(seconds=TOKEN_TOUCH_SECONDS)).isoformat(
        timespec="seconds"
    )
    if touch and (row["last_used_at"] is None or row["last_used_at"] < cutoff):
        try:
            _touch_token(conn, public_id, cutoff)
        except psycopg.errors.LockNotAvailable:
            log.warning("token last_used not saved id=%s", public_id)
    return owner, None


def revoke_token(conn: Connection, token_id: str) -> bool:
    with _write(conn):
        cur = conn.execute(
            "UPDATE api_tokens SET revoked_at=%s WHERE id=%s AND revoked_at IS NULL",
            (_now(), token_id),
        )
    return cur.rowcount > 0


def list_tokens(conn: Connection, owner_id: str | None = None) -> list[Row]:
    where, parameters = ("WHERE owner_id=%s", (owner_id,)) if owner_id is not None else ("", ())
    return conn.execute(
        f"SELECT {TOKEN_COLUMNS} FROM api_tokens {where} ORDER BY created_at DESC, seq DESC",
        parameters,
    ).fetchall()
