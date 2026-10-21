import asyncio
import json
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from itertools import islice
from time import monotonic
from typing import Annotated, Literal
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
from verifair.rules import compute_status


def require_token(request: Request) -> None:
    expected = f"Bearer {request.app.state.settings.api_token}"
    given = request.headers.get("Authorization", "")
    if not secrets.compare_digest(given.encode(), expected.encode()):
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


HttpUrl = Annotated[str, AfterValidator(_http_url)]
Author = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
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


def _current_sections(conn: sqlite3.Connection, id: str) -> list[Section]:
    return split_sections(db.get_version(conn, id, db.latest_version(conn, id))["body"])


def _comment_json(row: sqlite3.Row, resolved: bool | None = None) -> dict:
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
        "resolved": row["resolved_at"] is not None if resolved is None else resolved,
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
            author=body.author,
            agent=body.agent,
            model=body.model,
            session_url=body.session_url,
            commit=body.commit,
        )
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
) -> tuple[dict | None, dict[str, int]]:
    with closing(db.connect(path)) as conn:
        conn.execute("BEGIN")
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
            _poll_wait, request.app.state.settings.db_path, id, kind, since, after
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
        resolved = t.root["resolved_at"] is not None
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
                    _comment_json(r, resolved) | anchor | {"blocking": t.blocking}
                    for r in t.replies
                ]
            }
        )
    return {"threads": result}


@router.post("/reports/{id}/comments", status_code=201)
def post_comment(
    id: str, body: CommentIn, request: Request, conn: Conn, background: BackgroundTasks
):
    report = _require_report(conn, id)
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
    try:
        row = db.add_comment(
            conn,
            report_id=id,
            version=db.latest_version(conn, id),
            author_id="agent",
            author_name=body.author,
            body=body.body,
            parent_id=body.parent_id,
            section=body.section,
            quote=body.quote,
            blocking=body.blocking,
            prefix=body.prefix,
            suffix=body.suffix,
        )
    except ValueError:
        raise HTTPException(404, "parent not found") from None
    settings = request.app.state.settings
    background.add_task(notify.send, settings, notify.comment_text(report, row, settings.base_url))
    return _comment_json(row)


@router.post("/reports/{id}/comments/{cid}/resolve")
def resolve_comment(id: str, cid: int, conn: Conn):
    _require_report(conn, id)
    if db.resolve_thread(conn, id, cid) is None:
        raise HTTPException(404, "comment not found")
    return {"resolved": True}
