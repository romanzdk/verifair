import json
import secrets
import sqlite3
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from verifair import db, notify
from verifair.rules import compute_status


def require_token(request: Request) -> None:
    expected = f"Bearer {request.app.state.settings.api_token}"
    given = request.headers.get("Authorization", "")
    if not secrets.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "invalid token")


router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

Conn = Annotated[sqlite3.Connection, Depends(db.get_conn)]


class Link(BaseModel):
    label: str
    url: str


class ReportIn(BaseModel):
    project: str
    title: str = Field(min_length=1)
    risk: Literal["low", "medium", "high"]
    body: str = Field(min_length=1)
    id: str | None = None
    ticket_url: str | None = None
    links: list[Link] = []
    author: str = "agent"


class CommentIn(BaseModel):
    body: str = Field(min_length=1)
    author: str = "agent"
    parent_id: int | None = None
    section: str | None = None
    quote: str | None = None


def _require_report(conn: sqlite3.Connection, id: str) -> sqlite3.Row:
    report = db.get_report(conn, id)
    if report is None:
        raise HTTPException(404, "report not found")
    return report


def _comment_json(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "version": row["version"],
        "parent_id": row["parent_id"],
        "section": row["section"],
        "quote": row["quote"],
        "author": row["author_name"],
        "body": row["body"],
        "created_at": row["created_at"],
        "resolved": row["resolved_at"] is not None,
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
                "verdict": v["verdict"],
                "version": v["version"],
                "summary": v["summary"],
                "created_at": v["created_at"],
            }
            for v in verdicts
        ],
    }


@router.get("/reports/{id}/comments")
def get_comments(id: str, conn: Conn, open: bool = False):
    _require_report(conn, id)
    threads: dict[int, dict] = {}
    for row in db.list_comments(conn, id, open_only=open):
        if row["parent_id"] is None:
            threads[row["id"]] = _comment_json(row) | {"replies": []}
        else:
            threads[row["parent_id"]]["replies"].append(
                {
                    "id": row["id"],
                    "author": row["author_name"],
                    "body": row["body"],
                    "created_at": row["created_at"],
                }
            )
    return {"threads": [{k: v for k, v in t.items() if k != "parent_id"} for t in threads.values()]}


@router.post("/reports/{id}/comments", status_code=201)
def post_comment(
    id: str, body: CommentIn, request: Request, conn: Conn, background: BackgroundTasks
):
    report = _require_report(conn, id)
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
    if not db.resolve_thread(conn, id, cid):
        raise HTTPException(404, "comment not found")
    return {"resolved": True}
