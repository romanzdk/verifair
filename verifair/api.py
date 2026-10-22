import asyncio
import json
import logging
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from itertools import islice
from time import monotonic
from typing import Annotated, Literal, NoReturn
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints
from starlette.concurrency import run_in_threadpool

from verifair import db, notify
from verifair.deps import get_conn
from verifair.render import (
    Section,
    build_threads,
    iter_occurrences,
    normalize,
    section_headings,
    split_sections,
)
from verifair.rules import NO_OWNER, compute_status

NOT_OWNER = "This report belongs to another member."
SHARED_TOKEN = "The shared token is not accepted in shared mode. Use a member agent token."
AGENT_RESOLVE = "Agent tokens can only resolve threads the agent started on its owner's reports."
log = logging.getLogger("verifair")


def _refuse_token(reason: str, path: str, detail: str = "invalid token") -> NoReturn:
    log.info("api token refused reason=%s path=%s", reason, path)
    raise HTTPException(401, detail)


def require_token(request: Request) -> None:
    settings = request.app.state.settings
    given = request.headers.get("Authorization", "")
    request.state.token_owner = request.state.token = None
    if settings.identity_header:
        path = request.url.path
        scheme, _, token = given.partition(" ")
        if not given:
            _refuse_token("missing", path)
        if scheme != "Bearer" or not token:
            _refuse_token("malformed", path)
        if not token.lstrip().startswith("vf_"):
            _refuse_token("shared-token", path, SHARED_TOKEN)
        with closing(db.connect(settings.db_path)) as conn:
            owner, reason = db.authenticate_token(conn, token)
        if owner is None:
            _refuse_token(reason, path)
        request.state.token, request.state.token_owner = token, owner
        return
    token = settings.api_token
    if not token or not secrets.compare_digest(given.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "invalid token")


router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def _http_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("must be an http(s) URL")
    return url


def _not_blank(text: str) -> str:
    if not text.strip():
        raise ValueError("must not be blank")
    return text


def _human_id(value: str) -> str:
    if value == "agent":
        raise ValueError("must identify a human")
    return value


HttpUrl = Annotated[str, AfterValidator(_http_url)]
Author = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
UserId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
    AfterValidator(_human_id),
]
Anchor = Annotated[str, Field(max_length=2000)]
Context = Annotated[str, Field(max_length=64)]
SLUG = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"


class Link(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    url: HttpUrl


class ReportIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: str = Field(pattern=SLUG)
    title: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=200), AfterValidator(_not_blank)
    ]
    risk: Literal["low", "medium", "high"]
    body: Annotated[str, AfterValidator(_not_blank)]
    id: str | None = Field(default=None, pattern=SLUG)
    ticket_url: HttpUrl | None = None
    links: list[Link] = []
    author: Author = "agent"
    author_id: UserId | None = None
    reviewers: Annotated[list[UserId], Field(max_length=50)] | None = None
    agent: Annotated[str, Field(max_length=60)] | None = None
    model: Annotated[str, Field(max_length=60)] | None = None
    session_url: HttpUrl | None = None
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")


class CommentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
    author: Author = "agent"
    parent_id: int | None = None
    section: Anchor | None = None
    quote: Anchor | None = None
    prefix: Context | None = None
    suffix: Context | None = None
    blocking: bool = True


def _require_report(conn: sqlite3.Connection, id: str) -> sqlite3.Row:
    report = db.get_report(conn, id)
    if report is None:
        raise HTTPException(404, "report not found")
    return report


def _require_owner(report: sqlite3.Row, owner: sqlite3.Row | None) -> None:
    if owner is None:
        return
    if report["author_id"] is None:
        raise HTTPException(403, NO_OWNER)
    if report["author_id"] != owner["id"]:
        raise HTTPException(403, NOT_OWNER)


def _current_sections(conn: sqlite3.Connection, id: str) -> list[Section]:
    return split_sections(db.get_version(conn, id, db.latest_version(conn, id))["body"])


def _comment_json(row: sqlite3.Row, root: sqlite3.Row | None = None) -> dict:
    root = row if root is None else root
    return {
        "id": row["id"],
        "version": row["version"],
        "parent_id": row["parent_id"],
        "section": row["section"],
        "quote": row["quote"],
        "author": row["author_name"],
        "author_id": row["author_id"],
        "author_type": "agent" if row["author_id"] == "agent" else "human",
        "body": row["body"],
        "created_at": row["created_at"],
        "blocking": bool(row["blocking"]),
        "resolved": root["resolved_at"] is not None,
        "resolved_by": root["resolved_by"],
        "prefix": row["prefix"],
        "suffix": row["suffix"],
    }


def _verdict_json(row: sqlite3.Row, current: int) -> dict:
    return {
        "reviewer": row["reviewer_name"],
        "reviewer_id": row["reviewer_id"],
        "verdict": row["verdict"],
        "version": row["version"],
        "summary": row["summary"],
        "created_at": row["created_at"],
        "current": row["version"] == current,
    }


@router.post("/reports", status_code=201)
def push_report(body: ReportIn, request: Request, conn: Conn, background: BackgroundTasks):
    headings = set(section_headings(body.body))
    missing = [s for s in request.app.state.settings.required_sections if s not in headings]
    if missing:
        return JSONResponse({"detail": "missing sections", "sections": missing}, 422)
    owner = request.state.token_owner
    author = body.author if "author" in body.model_fields_set else None
    reviewers = body.reviewers
    if owner is not None:
        if body.author_id is not None and body.author_id.lower() != owner["id"]:
            raise HTTPException(403, "author_id must be the token owner.")
        if reviewers is not None:
            reviewers = [reviewer.lower() for reviewer in reviewers]
        inactive = []
        for reviewer in dict.fromkeys(reviewers or ()):
            member = db.get_user(conn, reviewer)
            if member is None or not member["active"]:
                inactive.append(reviewer)
        if inactive:
            return JSONResponse({"detail": "inactive reviewers", "reviewers": inactive}, 422)
        author = owner["name"]
    try:
        id, version = db.push_report(
            conn,
            project=body.project,
            title=body.title,
            risk=body.risk,
            body=body.body,
            id=body.id,
            ticket_url=body.ticket_url,
            links=[link.model_dump() for link in body.links],
            author=author,
            author_id=body.author_id,
            reviewers=reviewers,
            agent=body.agent,
            model=body.model,
            session_url=body.session_url,
            commit=body.commit,
            owner_id=owner["id"] if owner is not None else None,
        )
    except db.NoOwner:
        raise HTTPException(403, NO_OWNER) from None
    except db.NotOwner:
        raise HTTPException(403, NOT_OWNER) from None
    except db.ProjectMismatch:
        raise HTTPException(409, "id belongs to another project") from None
    settings = request.app.state.settings
    base_url = settings.base_url
    background.add_task(
        notify.send, settings, notify.report_pushed_text(db.get_report(conn, id), version, base_url)
    )
    return {"id": id, "version": version, "url": f"{base_url}/r/{id}"}


@router.get("/reports/{id}")
def get_report(id: str, conn: Conn):
    report = _require_report(conn, id)
    current = db.latest_version(conn, id)
    verdicts = db.list_verdicts(conn, id)
    signature = db.get_signature(conn, id, current)
    return {
        "id": report["id"],
        "project": report["project"],
        "title": report["title"],
        "risk": report["risk"],
        "ticket_url": report["ticket_url"],
        "links": json.loads(report["links"]),
        "author": report["author"],
        "author_id": report["author_id"],
        "reviewers": db.requested_reviewers(conn, id),
        "agent": report["agent"],
        "model": report["model"],
        "session_url": report["session_url"],
        "signed_by": signature["signer_name"] if signature else None,
        "signed_at": signature["created_at"] if signature else None,
        "status": compute_status(
            verdicts,
            current,
            signed=signature is not None,
            signer_id=signature["signer_id"] if signature else None,
        ),
        "current_version": current,
        "versions": [
            {"version": v["version"], "created_at": v["created_at"], "commit": v["commit"]}
            for v in db.list_versions(conn, id)
        ],
        "verdicts": [_verdict_json(v, current) for v in verdicts],
    }


def _poll_wait(
    path: str,
    id: str,
    kind: str,
    since: datetime | None,
    after: dict[str, int] | None,
    token: str | None,
) -> tuple[dict | None, dict[str, int]]:
    with closing(db.connect(path)) as conn:
        conn.execute("BEGIN")
        if token is not None and (reason := db.authenticate_token(conn, token)[1]):
            _refuse_token(reason, f"/api/reports/{id}/wait")
        _require_report(conn, id)
        current = db.latest_version(conn, id)
        rows = {}
        if kind in ("any", "verdict"):
            rows["verdict"] = db.list_verdicts(conn, id)
        if kind in ("any", "comment"):
            rows["comment"] = db.list_comments(conn, id)
        if since is None and after is None:
            return None, {
                event: max((r["id"] for r in items), default=0) for event, items in rows.items()
            }
        events = []
        for event, items in rows.items():
            for row in items:
                if row["version"] != current or (
                    event == "comment" and row["parent_id"] is not None
                ):
                    continue
                created = datetime.fromisoformat(row["created_at"])
                if since is not None:
                    if created <= since:
                        continue
                elif row["id"] <= after[event]:
                    continue
                value = _verdict_json(row, current) if event == "verdict" else _comment_json(row)
                events.append(
                    (created, {"event": event, event: value, "next_since": row["created_at"]})
                )
        return min(events, key=lambda e: e[0])[1] if events else None, after or {}


@router.get("/reports/{id}/wait")
async def wait_for_event(
    id: str,
    request: Request,
    kind: Annotated[Literal["verdict", "comment", "any"], Query(alias="for")] = "any",
    since: datetime | None = None,
    timeout: Annotated[float, Query(gt=0, le=55)] = 55,
):
    if since is not None:
        since = since.replace(tzinfo=UTC) if since.tzinfo is None else since.astimezone(UTC)
    cursor = (since or datetime.now(UTC)).isoformat()
    deadline = monotonic() + timeout
    after = None
    while True:
        event, after = await run_in_threadpool(
            _poll_wait,
            request.app.state.settings.db_path,
            id,
            kind,
            since,
            after,
            request.state.token,
        )
        if event is not None:
            return {"since": cursor, **event}
        remaining = deadline - monotonic()
        if remaining <= 0:
            return {"event": "timeout", "since": cursor, "next_since": cursor}
        await asyncio.sleep(min(0.5, remaining))


@router.get("/reports/{id}/comments")
def get_comments(id: str, conn: Conn, open: bool = False):
    _require_report(conn, id)
    comments = db.list_comments(conn, id, open_only=open)
    threads = build_threads(comments, _current_sections(conn, id), db.latest_version(conn, id))
    result = []
    for t in threads:
        anchor = {
            "stale": t.stale,
            "found": t.found if t.root["quote"] else True,
            "moved": t.moved,
            "match_start": t.match_start,
        }
        result.append(
            _comment_json(t.root)
            | anchor
            | {
                "replies": [
                    _comment_json(r, t.root) | anchor | {"blocking": t.blocking} for r in t.replies
                ],
            }
        )
    return {"threads": result}


@router.post("/reports/{id}/comments", status_code=201)
def post_comment(
    id: str, body: CommentIn, request: Request, conn: Conn, background: BackgroundTasks
):
    report = _require_report(conn, id)
    owner = request.state.token_owner
    _require_owner(report, owner)
    if body.parent_id is not None and any(
        value is not None for value in (body.section, body.quote, body.prefix, body.suffix)
    ):
        raise HTTPException(422, "replies cannot set section, quote, prefix or suffix")
    if body.parent_id is None and body.quote is not None and body.section is None:
        raise HTTPException(422, "quote requires section")
    if body.parent_id is None and body.section is not None:
        sections = _current_sections(conn, id)
        headings = list(dict.fromkeys(s.heading for s in sections))
        if body.section not in headings:
            return JSONResponse({"detail": "unknown section", "sections": headings}, 422)
        quote = normalize(body.quote or "")
        if quote and not (body.prefix or body.suffix):
            same = [s for s in sections if s.heading == body.section]
            hits = sum(
                1
                for _ in islice(
                    (start for s in same for start in iter_occurrences(s.text, quote)), 2
                )
            )
            if hits > 1:
                raise HTTPException(422, "ambiguous quote")
    reopen = True
    if owner is not None and body.parent_id is not None:
        root = db.thread_root(conn, id, body.parent_id)
        reopen = root is not None and root["author_id"] == "agent"
    try:
        row = db.add_comment(
            conn,
            report_id=id,
            version=db.latest_version(conn, id),
            author_id="agent",
            author_name=f"{owner['name']}'s agent" if owner is not None else body.author,
            body=body.body,
            parent_id=body.parent_id,
            section=body.section,
            quote=body.quote,
            blocking=body.blocking,
            prefix=body.prefix,
            suffix=body.suffix,
            reopen=reopen,
        )
    except ValueError:
        raise HTTPException(404, "parent not found") from None
    settings = request.app.state.settings
    background.add_task(notify.send, settings, notify.comment_text(report, row, settings.base_url))
    return _comment_json(row, db.thread_root(conn, id, row["id"]))


@router.post("/reports/{id}/comments/{cid}/resolve")
def resolve_comment(id: str, cid: int, request: Request, conn: Conn):
    report = _require_report(conn, id)
    owner = request.state.token_owner
    by = None
    if owner is not None:
        _require_owner(report, owner)
        root = db.thread_root(conn, id, cid)
        if root is None:
            raise HTTPException(404, "comment not found")
        if root["author_id"] != "agent":
            raise HTTPException(403, AGENT_RESOLVE)
        by = "agent"
    if db.resolve_thread(conn, id, cid, by=by) is None:
        raise HTTPException(404, "comment not found")
    return {"resolved": True}
