import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from verifair.render import slugify

log = logging.getLogger("verifair")

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    title TEXT NOT NULL,
    risk TEXT NOT NULL CHECK (risk IN ('low', 'medium', 'high')),
    ticket_url TEXT,
    links TEXT NOT NULL DEFAULT '[]',
    author TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    agent TEXT,
    model TEXT,
    session_url TEXT
);
CREATE TABLE IF NOT EXISTS versions (
    report_id TEXT NOT NULL REFERENCES reports(id),
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    "commit" TEXT,
    PRIMARY KEY (report_id, version)
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES reports(id),
    version INTEGER NOT NULL,
    parent_id INTEGER REFERENCES comments(id),
    section TEXT,
    quote TEXT,
    author_id TEXT NOT NULL,
    author_name TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    blocking INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY,
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
CREATE INDEX IF NOT EXISTS comments_report_parent ON comments(report_id, parent_id);
CREATE INDEX IF NOT EXISTS verdicts_report ON verdicts(report_id);
CREATE INDEX IF NOT EXISTS reports_project ON reports(project);
"""


class ProjectMismatch(Exception):
    pass


class AlreadySigned(Exception):
    pass


class StaleVersion(Exception):
    def __init__(self, latest: int) -> None:
        super().__init__(f"latest version is {latest}")
        self.latest = latest


def _short(user_id: str) -> str:
    return f"{user_id[:4]}…"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: str) -> sqlite3.Connection:
    # FastAPI may run a dependency's setup, the endpoint and teardown on different threads
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def _write(conn: sqlite3.Connection) -> Iterator[None]:
    # IMMEDIATE takes the write lock before the reads, so concurrent pushes cannot pick the same
    # version number or slug
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        yield


def _check_version(conn: sqlite3.Connection, report_id: str, expected: int | None) -> None:
    if expected is not None and (latest := latest_version(conn, report_id)) != expected:
        raise StaleVersion(latest)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN "{column}" {decl}')


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_column(conn, "reports", "agent", "TEXT")
    _ensure_column(conn, "reports", "model", "TEXT")
    _ensure_column(conn, "reports", "session_url", "TEXT")
    _ensure_column(conn, "versions", "commit", "TEXT")
    _ensure_column(conn, "comments", "blocking", "INTEGER NOT NULL DEFAULT 1")


def _unique_id(conn: sqlite3.Connection, base: str) -> str:
    candidate, n = base, 1
    while get_report(conn, candidate):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def push_report(
    conn: sqlite3.Connection,
    *,
    project: str,
    title: str,
    risk: str,
    body: str,
    id: str | None = None,
    ticket_url: str | None = None,
    links: list[str] | None = None,
    author: str = "agent",
    agent: str | None = None,
    model: str | None = None,
    session_url: str | None = None,
    commit: str | None = None,
) -> tuple[str, int]:
    now = _now()
    links_json = json.dumps(links or [])
    with _write(conn):
        existing = get_report(conn, id) if id else None
        if existing:
            if existing["project"] != project:
                log.info(
                    "push rejected id=%s project=%s owner=%s", id, project, existing["project"]
                )
                raise ProjectMismatch(f"report {id!r} belongs to project {existing['project']!r}")
            version = latest_version(conn, id) + 1
            conn.execute(
                "UPDATE reports SET title=?, risk=?, ticket_url=?, links=?, author=?, updated_at=?,"
                " agent=?, model=?, session_url=? WHERE id=?",
                (title, risk, ticket_url, links_json, author, now, agent, model, session_url, id),
            )
        else:
            id = id or _unique_id(conn, slugify(title))
            version = 1
            conn.execute(
                "INSERT INTO reports (id, project, title, risk, ticket_url, links, author,"
                " created_at, updated_at, agent, model, session_url)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    id,
                    project,
                    title,
                    risk,
                    ticket_url,
                    links_json,
                    author,
                    now,
                    now,
                    agent,
                    model,
                    session_url,
                ),
            )
        conn.execute(
            'INSERT INTO versions (report_id, version, body, created_at, "commit")'
            " VALUES (?, ?, ?, ?, ?)",
            (id, version, body, now, commit),
        )
    log.info("report pushed id=%s project=%s version=%d", id, project, version)
    return id, version


def get_report(conn: sqlite3.Connection, id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM reports WHERE id=?", (id,)).fetchone()


def get_version(conn: sqlite3.Connection, id: str, version: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM versions WHERE report_id=? AND version=?", (id, version)
    ).fetchone()


def latest_version(conn: sqlite3.Connection, id: str) -> int:
    row = conn.execute("SELECT MAX(version) FROM versions WHERE report_id=?", (id,)).fetchone()
    return row[0] or 0


def list_versions(conn: sqlite3.Connection, id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM versions WHERE report_id=? ORDER BY version", (id,)
    ).fetchall()


def list_projects(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT project FROM reports ORDER BY project").fetchall()
    return [r["project"] for r in rows]


def list_reports(conn: sqlite3.Connection, project: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT r.*,"
        " (SELECT MAX(version) FROM versions v WHERE v.report_id = r.id) AS latest_version,"
        " (SELECT COUNT(*) FROM comments c WHERE c.report_id = r.id"
        "  AND c.parent_id IS NULL AND c.resolved_at IS NULL) AS open_threads"
        " FROM reports r WHERE r.project=? ORDER BY r.created_at DESC, r.rowid DESC",
        (project,),
    ).fetchall()


def add_comment(
    conn: sqlite3.Connection,
    *,
    report_id: str,
    version: int,
    author_id: str,
    author_name: str,
    body: str,
    parent_id: int | None = None,
    section: str | None = None,
    quote: str | None = None,
    expected_version: int | None = None,
    blocking: bool = True,
) -> sqlite3.Row:
    with _write(conn):
        _check_version(conn, report_id, expected_version)
        if parent_id is not None:
            parent = conn.execute(
                "SELECT * FROM comments WHERE id=? AND report_id=?", (parent_id, report_id)
            ).fetchone()
            if parent is None:
                raise ValueError(f"parent comment {parent_id} not found on report {report_id!r}")
            parent_id = parent["parent_id"] or parent["id"]
            version, section, quote = parent["version"], parent["section"], parent["quote"]
            blocking = bool(
                conn.execute("SELECT blocking FROM comments WHERE id=?", (parent_id,)).fetchone()[0]
            )
            reopen_thread(conn, report_id, parent_id)
        cur = conn.execute(
            "INSERT INTO comments (report_id, version, parent_id, section, quote, author_id,"
            " author_name, body, created_at, blocking) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
    log.info(
        "comment added id=%d report=%s version=%d parent=%s author_id=%s",
        cur.lastrowid,
        report_id,
        version,
        parent_id,
        _short(author_id),
    )
    return conn.execute("SELECT * FROM comments WHERE id=?", (cur.lastrowid,)).fetchone()


def list_comments(
    conn: sqlite3.Connection, report_id: str, open_only: bool = False
) -> list[sqlite3.Row]:
    sql = (
        "SELECT c.* FROM comments c JOIN comments root ON root.id = COALESCE(c.parent_id, c.id)"
        " WHERE c.report_id=?"
    )
    if open_only:
        sql += " AND root.resolved_at IS NULL"
    return conn.execute(sql + " ORDER BY c.id", (report_id,)).fetchall()


def resolve_thread(conn: sqlite3.Connection, report_id: str, comment_id: int) -> int | None:
    with _write(conn):
        row = conn.execute(
            "SELECT COALESCE(parent_id, id) FROM comments WHERE id=? AND report_id=?",
            (comment_id, report_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE comments SET resolved_at=? WHERE id=? AND resolved_at IS NULL",
            (_now(), row[0]),
        )
    return row[0]


def reopen_thread(conn: sqlite3.Connection, report_id: str, comment_id: int) -> bool:
    cur = conn.execute(
        "UPDATE comments SET resolved_at=NULL WHERE report_id=? AND id ="
        " (SELECT COALESCE(parent_id, id) FROM comments WHERE id=? AND report_id=?)",
        (report_id, comment_id, report_id),
    )
    return cur.rowcount > 0


def add_verdict(
    conn: sqlite3.Connection,
    *,
    report_id: str,
    version: int,
    reviewer_id: str,
    reviewer_name: str,
    verdict: str,
    summary: str = "",
    expected_version: int | None = None,
) -> sqlite3.Row:
    with _write(conn):
        _check_version(conn, report_id, expected_version)
        cur = conn.execute(
            "INSERT INTO verdicts (report_id, version, reviewer_id, reviewer_name, verdict,"
            " summary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (report_id, version, reviewer_id, reviewer_name, verdict, summary, _now()),
        )
    log.info(
        "verdict added report=%s version=%d reviewer_id=%s verdict=%s",
        report_id,
        version,
        _short(reviewer_id),
        verdict,
    )
    return conn.execute("SELECT * FROM verdicts WHERE id=?", (cur.lastrowid,)).fetchone()


def list_verdicts(conn: sqlite3.Connection, report_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM verdicts WHERE report_id=? ORDER BY id", (report_id,)
    ).fetchall()


def set_blocking(conn: sqlite3.Connection, report_id: str, comment_id: int, blocking: bool) -> bool:
    with _write(conn):
        cur = conn.execute(
            "UPDATE comments SET blocking=? WHERE id=? AND report_id=? AND parent_id IS NULL",
            (int(blocking), comment_id, report_id),
        )
    return cur.rowcount > 0


def sign_version(
    conn: sqlite3.Connection, *, report_id: str, version: int, signer_id: str, signer_name: str
) -> sqlite3.Row:
    with _write(conn):
        _check_version(conn, report_id, version)
        try:
            conn.execute(
                "INSERT INTO signatures (report_id, version, signer_id, signer_name, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (report_id, version, signer_id, signer_name, _now()),
            )
        except sqlite3.IntegrityError:
            raise AlreadySigned(f"{report_id} v{version} is already signed") from None
    log.info(
        "version signed report=%s version=%d signer_id=%s", report_id, version, _short(signer_id)
    )
    return get_signature(conn, report_id, version)


def get_signature(conn: sqlite3.Connection, report_id: str, version: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM signatures WHERE report_id=? AND version=?", (report_id, version)
    ).fetchone()
