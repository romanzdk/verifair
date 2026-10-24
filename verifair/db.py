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
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from verifair.render import slugify

log = logging.getLogger("verifair")

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


def connect(url: str) -> Connection:
    return psycopg.connect(url, **CONNECT_KWARGS)


def open_pool(url: str, max_size: int) -> ConnectionPool:
    pool = ConnectionPool(
        url,
        min_size=1,
        max_size=max_size,
        timeout=10,
        kwargs=CONNECT_KWARGS,
        check=ConnectionPool.check_connection,
        open=False,
    )
    pool.open(wait=True, timeout=10)
    return pool


def _lock_report(conn: Connection, id: str) -> Row | None:
    return conn.execute("SELECT * FROM reports WHERE id=%s FOR UPDATE", (id,)).fetchone()


@contextmanager
def _write(conn: Connection, report_id: str | None = None) -> Iterator[None]:
    # writes on one report hold its row lock, so version checks, validate() and the insert
    # see no concurrent change to that report until commit
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


def push_report(
    conn: Connection,
    *,
    project: str,
    title: str,
    risk: str,
    body: str,
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
) -> tuple[str, int]:
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
        if reviewers is not None:
            conn.execute("DELETE FROM review_requests WHERE report_id=%s", (id,))
            conn.cursor().executemany(
                "INSERT INTO review_requests (report_id, reviewer_id) VALUES (%s, %s)",
                [(id, reviewer) for reviewer in set(reviewers)],
            )
        conn.execute(
            'INSERT INTO versions (report_id, version, body, created_at, "commit")'
            " VALUES (%s, %s, %s, %s, %s)",
            (id, version, body, now, commit),
        )
    log.info("report pushed id=%s project=%s version=%d", id, project, version)
    return id, version


def get_report(conn: Connection, id: str) -> Row | None:
    return conn.execute("SELECT * FROM reports WHERE id=%s", (id,)).fetchone()


def get_version(conn: Connection, id: str, version: int) -> Row | None:
    return conn.execute(
        "SELECT * FROM versions WHERE report_id=%s AND version=%s", (id, version)
    ).fetchone()


def latest_version(conn: Connection, id: str) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM versions WHERE report_id=%s", (id,)
    ).fetchone()
    return row["v"] or 0


def list_versions(conn: Connection, id: str) -> list[Row]:
    return conn.execute(
        "SELECT * FROM versions WHERE report_id=%s ORDER BY version", (id,)
    ).fetchall()


def project_exists(conn: Connection, project: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM reports WHERE project=%s LIMIT 1", (project,)).fetchone()
        is not None
    )


def list_reports(conn: Connection) -> list[Row]:
    return conn.execute(
        "SELECT r.*, s.signer_id, s.created_at AS signed_at,"
        " (SELECT MAX(version) FROM versions v WHERE v.report_id = r.id) AS latest_version,"
        " (SELECT COUNT(*) FROM comments c WHERE c.report_id = r.id"
        "  AND c.parent_id IS NULL AND c.resolved_at IS NULL) AS open_threads"
        " FROM reports r LEFT JOIN signatures s ON s.report_id = r.id"
        " AND s.version = (SELECT MAX(version) FROM versions v WHERE v.report_id = r.id)"
        " ORDER BY r.created_at DESC, r.seq DESC"
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
        "SELECT id, report_id, version, reviewer_id, reviewer_name, verdict, created_at"
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


def add_comment(
    conn: Connection,
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
    prefix: str | None = None,
    suffix: str | None = None,
    reopen: bool = True,
) -> Row:
    with _write(conn, report_id):
        _check_version(conn, report_id, expected_version)
        if parent_id is not None:
            parent = conn.execute(
                "SELECT * FROM comments WHERE id=%s AND report_id=%s", (parent_id, report_id)
            ).fetchone()
            if parent is None:
                raise ValueError(f"parent comment {parent_id} not found on report {report_id!r}")
            parent_id = parent["parent_id"] or parent["id"]
            version, section, quote = parent["version"], parent["section"], parent["quote"]
            prefix, suffix = parent["prefix"], parent["suffix"]
            root = conn.execute(
                "SELECT blocking FROM comments WHERE id=%s", (parent_id,)
            ).fetchone()
            blocking = bool(root["blocking"])
            if reopen:
                reopen_thread(conn, report_id, parent_id)
        row = conn.execute(
            "INSERT INTO comments (report_id, version, parent_id, section, quote, author_id,"
            " author_name, body, created_at, blocking, prefix, suffix)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
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


def list_comments(conn: Connection, report_id: str, open_only: bool = False) -> list[Row]:
    sql = (
        "SELECT c.* FROM comments c JOIN comments root ON root.id = COALESCE(c.parent_id, c.id)"
        " WHERE c.report_id=%s"
    )
    if open_only:
        sql += " AND root.resolved_at IS NULL"
    return conn.execute(sql + " ORDER BY c.id", (report_id,)).fetchall()


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
    reviewer_id: str,
    reviewer_name: str,
    verdict: str,
    summary: str = "",
    expected_version: int | None = None,
    validate: Callable[[], str | None] | None = None,
) -> Row:
    with _write(conn, report_id):
        _check_version(conn, report_id, expected_version)
        if validate is not None and (error := validate()):
            raise VerdictRefused(error)
        row = conn.execute(
            "INSERT INTO verdicts (report_id, version, reviewer_id, reviewer_name, verdict,"
            " summary, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (report_id, version, reviewer_id, reviewer_name, verdict, summary, _now()),
        ).fetchone()
    log.info(
        "verdict added report=%s version=%d reviewer_id=%s verdict=%s",
        report_id,
        version,
        _short(reviewer_id),
        verdict,
    )
    return row


def list_verdicts(conn: Connection, report_id: str) -> list[Row]:
    return conn.execute(
        "SELECT * FROM verdicts WHERE report_id=%s ORDER BY id", (report_id,)
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
    conn: Connection, *, report_id: str, version: int, signer_id: str, signer_name: str
) -> Row:
    with _write(conn, report_id):
        _check_version(conn, report_id, version)
        signed = conn.execute(
            "INSERT INTO signatures (report_id, version, signer_id, signer_name, created_at)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (report_id, version) DO NOTHING RETURNING report_id",
            (report_id, version, signer_id, signer_name, _now()),
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
    return get_signature(conn, report_id, version)


def get_signature(conn: Connection, report_id: str, version: int) -> Row | None:
    return conn.execute(
        "SELECT * FROM signatures WHERE report_id=%s AND version=%s", (report_id, version)
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
    # local mode stores ids as sent, shared mode matches them to the lower-cased identity
    # header. Python's lower() is how the header is read. An old human id such as "Agent"
    # stays as it is so it does not turn into the agent.
    changed = 0
    with _write(conn):
        for table, column in HUMAN_ID_COLUMNS:
            for row in conn.execute(
                f"SELECT DISTINCT {column} AS id FROM {table} WHERE {column} IS NOT NULL"
            ).fetchall():
                old = row["id"]
                new = old.lower()
                if new in (old, "agent"):
                    continue
                if table == "review_requests":
                    conn.execute(
                        "INSERT INTO review_requests (report_id, reviewer_id)"
                        " SELECT report_id, %s FROM review_requests WHERE reviewer_id=%s"
                        " ON CONFLICT DO NOTHING",
                        (new, old),
                    )
                    sql = "DELETE FROM review_requests WHERE reviewer_id=%s"
                    changed += conn.execute(sql, (old,)).rowcount
                else:
                    sql = f"UPDATE {table} SET {column}=%s WHERE {column}=%s"
                    changed += conn.execute(sql, (new, old)).rowcount
    return changed


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
