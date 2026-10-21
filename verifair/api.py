import json
import secrets
import sqlite3
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from verifair import db, notify
from verifair.deps import get_conn
from verifair.render import Section, build_threads, split_sections
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


class CommentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
    author: Author = "agent"
    parent_id: int | None = None
    section: Anchor | None = None
    quote: Anchor | None = None


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
        "resolved": row["resolved_at"] is not None if resolved is None else resolved,
    }


@router.post("/reports", status_code=201)
def push_report(body: ReportIn, request: Request, conn: Conn, background: BackgroundTasks):
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
    return {
        "id": report["id"],
        "project": report["project"],
        "title": report["title"],
        "risk": report["risk"],
        "ticket_url": report["ticket_url"],
        "links": json.loads(report["links"]),
        "author": report["author"],
        "status": compute_status(verdicts, current),
        "current_version": current,
        "versions": [
            {"version": v["version"], "created_at": v["created_at"]}
            for v in db.list_versions(conn, id)
        ],
        "verdicts": [
            {
                "reviewer": v["reviewer_name"],
                "reviewer_id": v["reviewer_id"],
                "verdict": v["verdict"],
                "version": v["version"],
                "summary": v["summary"],
                "created_at": v["created_at"],
                "current": v["version"] == current,
            }
            for v in verdicts
        ],
    }


@router.get("/reports/{id}/comments")
def get_comments(id: str, conn: Conn, open: bool = False):
    _require_report(conn, id)
    comments = db.list_comments(conn, id, open_only=open)
    threads = build_threads(comments, _current_sections(conn, id), db.latest_version(conn, id))
    result = []
    for t in threads:
        resolved = t.root["resolved_at"] is not None
        anchor = {"stale": t.stale, "found": t.found if t.root["quote"] else True}
        result.append(
            _comment_json(t.root)
            | anchor
            | {"replies": [_comment_json(r, resolved) | anchor for r in t.replies]}
        )
    return {"threads": result}


@router.post("/reports/{id}/comments", status_code=201)
def post_comment(
    id: str, body: CommentIn, request: Request, conn: Conn, background: BackgroundTasks
):
    report = _require_report(conn, id)
    if body.parent_id is not None and (body.section is not None or body.quote is not None):
        raise HTTPException(422, "replies cannot set section or quote")
    if body.parent_id is None and body.quote is not None and body.section is None:
        raise HTTPException(422, "quote requires section")
    if body.parent_id is None and body.section is not None:
        headings = list(dict.fromkeys(s.heading for s in _current_sections(conn, id)))
        if body.section not in headings:
            return JSONResponse({"detail": "unknown section", "sections": headings}, 422)
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
