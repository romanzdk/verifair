import json
import re
import secrets
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote as url_quote
from urllib.parse import unquote

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from verifair import db, notify, rules
from verifair.deps import get_conn
from verifair.render import build_threads, split_sections, threads_by_section

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _date(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{d.day} {d:%b %Y}"


templates.env.filters["date"] = _date
templates.env.globals["STATUS_LABELS"] = rules.STATUS_LABELS

Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
YEAR = 365 * 24 * 3600
USER_ID = re.compile(r"[A-Za-z0-9_-]{8,16}")
TABS = ("awaiting_review", "changes_requested", "approved")
REFUSED = "Add a summary, or start a comment thread of your own and leave it open."
Body = Annotated[str, Form(max_length=20000)]
Anchor = Annotated[str, Form(max_length=2000)]


def _header_user(request: Request) -> tuple[str, str] | None:
    header = request.app.state.settings.identity_header
    value = request.headers.get(header, "").strip() if header else ""
    if not value:
        return None
    return value, value.split("@")[0] or value


def current_user(request: Request) -> tuple[str, str | None]:
    from_header = _header_user(request)
    request.state.user_from_header = from_header is not None
    if from_header:
        return from_header
    user_id = request.cookies.get("vf_id")
    if not user_id or user_id == "agent" or not USER_ID.fullmatch(user_id):
        # FastAPI drops cookies set on an injected Response when the route returns its own
        user_id = request.state.new_vf_id = secrets.token_urlsafe(8)
    name = request.cookies.get("vf_name")
    return user_id, unquote(name) if name else None


User = Annotated[tuple[str, str | None], Depends(current_user)]


def _secure(request: Request) -> bool:
    return request.app.state.settings.base_url.startswith("https")


def _with_id_cookie(request: Request, response):
    new_id = getattr(request.state, "new_vf_id", None)
    if new_id:
        response.set_cookie(
            "vf_id", new_id, max_age=YEAR, samesite="lax", httponly=True, secure=_secure(request)
        )
    return response


def _page(request: Request, user: tuple[str, str | None], name: str, ctx: dict):
    url = request.url.remove_query_params("name_error")
    ctx = ctx | {
        "user_name": user[1],
        "user_from_header": request.state.user_from_header,
        "here": url.path + (f"?{url.query}" if url.query else ""),
        "name_error": "name_error" in request.query_params,
    }
    return _with_id_cookie(request, templates.TemplateResponse(request, name, ctx))


def _partial(request: Request, names: list[str], ctx: dict, headers: dict | None = None):
    html = "".join(templates.get_template(n).render(ctx) for n in names)
    return _with_id_cookie(request, HTMLResponse(html, headers=headers))


def _retarget(request: Request, target: str | None = None) -> dict | None:
    trigger = request.headers.get("HX-Trigger")
    target = target or (f"#{trigger}" if trigger else None)
    return {"HX-Retarget": target, "HX-Reswap": "outerHTML"} if target else None


def _name_prompt(request: Request, next: str, target: str | None = None):
    return _partial(request, ["_name_prompt.html"], {"next": next}, _retarget(request, target))


def _stale(request: Request, id: str, latest: int):
    ctx = {"url": f"/r/{url_quote(id)}", "latest": latest}
    return _partial(request, ["_stale.html"], ctx, _retarget(request))


def _require_body(body: str) -> str:
    body = body.strip()
    if not body:
        raise HTTPException(422, "comment body is empty")
    return body


def _require_report(conn: sqlite3.Connection, id: str) -> sqlite3.Row:
    report = db.get_report(conn, id)
    if report is None:
        raise HTTPException(404, "report not found")
    return report


def _ticket_label(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _links(report: sqlite3.Row) -> list[tuple[str, str]]:
    links = [(link["label"], link["url"]) for link in json.loads(report["links"])]
    if report["ticket_url"]:
        links.insert(0, (_ticket_label(report["ticket_url"]), report["ticket_url"]))
    return links


def report_context(
    conn: sqlite3.Connection, report: sqlite3.Row, version: int, user_id: str
) -> dict:
    id = report["id"]
    current = db.latest_version(conn, id)
    shown = db.get_version(conn, id, version)
    sections = split_sections(shown["body"])
    comments = db.list_comments(conn, id)
    verdicts = db.list_verdicts(conn, id)
    threads = build_threads(comments, sections, current)
    open_threads = [t for t in threads if t.root["resolved_at"] is None]
    return {
        "report": report,
        "url": f"/r/{url_quote(id)}",
        "ticket": _ticket_label(report["ticket_url"]) if report["ticket_url"] else None,
        "version": version,
        "version_date": shown["created_at"],
        "current_version": current,
        "is_current": version == current,
        "status": rules.compute_status(verdicts, current),
        "sections": sections,
        "threads": threads,
        "threads_by_section": threads_by_section(threads),
        "open_count": len(open_threads),
        "open_by_section": Counter(t.section_slug for t in open_threads),
        "reviewers": rules.reviewers(comments, verdicts),
        "versions": db.list_versions(conn, id)[::-1],
        "links": _links(report),
        "my_verdict": rules.latest_verdicts(verdicts, current).get(user_id),
        "user_id": user_id,
    }


def _thread_response(request, conn, report, user_id, comment_id, reset_form=False):
    ctx = report_context(conn, report, db.latest_version(conn, report["id"]), user_id)
    ctx["thread"] = next(
        t for t in ctx["threads"] if comment_id in (t.root["id"], *(r["id"] for r in t.replies))
    )
    ctx["oob"] = True
    names = ["_thread.html", "_sidebar.html"]
    section = next((s for s in ctx["sections"] if s.slug == ctx["thread"].section_slug), None)
    if reset_form and section:
        ctx["s"] = section
        names.append("_cform.html")
    return _partial(request, names, ctx)


@router.get("/")
def index(request: Request, conn: Conn, user: User):
    projects = []
    for project in db.list_projects(conn):
        statuses = Counter(
            rules.compute_status(db.list_verdicts(conn, r["id"]), r["latest_version"])
            for r in db.list_reports(conn, project)
        )
        projects.append({"name": project, "counts": statuses})
    return _page(request, user, "index.html", {"projects": projects})


@router.get("/p/{project}")
def project_page(
    project: str,
    request: Request,
    conn: Conn,
    user: User,
    tab: str = "awaiting_review",
):
    if tab not in (*TABS, "all"):
        tab = "awaiting_review"
    reports = db.list_reports(conn, project)
    if not reports:
        raise HTTPException(404, "project not found")
    rows = [
        {
            "report": r,
            "status": rules.compute_status(db.list_verdicts(conn, r["id"]), r["latest_version"]),
            "ticket": _ticket_label(r["ticket_url"]) if r["ticket_url"] else None,
        }
        for r in reports
    ]
    ctx = {
        "project": project,
        "tab": tab,
        "tabs": TABS,
        "counts": Counter(row["status"] for row in rows),
        "total": len(rows),
        "rows": [row for row in rows if tab == "all" or row["status"] == tab],
    }
    return _page(request, user, "project.html", ctx)


@router.get("/r/{id}")
def report_page(id: str, request: Request, conn: Conn, user: User):
    report = _require_report(conn, id)
    ctx = report_context(conn, report, db.latest_version(conn, id), user[0])
    return _page(request, user, "report.html", ctx)


@router.get("/r/{id}/v/{n}")
def report_version_page(id: str, n: int, request: Request, conn: Conn, user: User):
    report = _require_report(conn, id)
    if db.get_version(conn, id, n) is None:
        raise HTTPException(404, "version not found")
    return _page(request, user, "report.html", report_context(conn, report, n, user[0]))


@router.post("/r/{id}/comments")
def post_comment(
    id: str,
    request: Request,
    conn: Conn,
    user: User,
    background: BackgroundTasks,
    version: Annotated[int, Form()],
    section: Anchor,
    body: Body,
    quote: Annotated[str | None, Form(max_length=2000)] = None,
):
    report = _require_report(conn, id)
    body = _require_body(body)
    user_id, name = user
    if not name:
        return _name_prompt(request, f"/r/{id}")
    try:
        row = db.add_comment(
            conn,
            report_id=id,
            version=version,
            expected_version=version,
            author_id=user_id,
            author_name=name,
            body=body,
            section=section,
            quote=quote or None,
        )
    except db.StaleVersion as stale:
        return _stale(request, id, stale.latest)
    settings = request.app.state.settings
    background.add_task(notify.send, settings, notify.comment_text(report, row, settings.base_url))
    return _thread_response(request, conn, report, user_id, row["id"], reset_form=True)


@router.post("/r/{id}/comments/{cid}/reply")
def post_reply(
    id: str,
    cid: int,
    request: Request,
    conn: Conn,
    user: User,
    background: BackgroundTasks,
    version: Annotated[int, Form()],
    body: Body,
):
    report = _require_report(conn, id)
    body = _require_body(body)
    user_id, name = user
    if not name:
        return _name_prompt(request, f"/r/{id}")
    try:
        row = db.add_comment(
            conn,
            report_id=id,
            version=version,
            expected_version=version,
            author_id=user_id,
            author_name=name,
            body=body,
            parent_id=cid,
        )
    except db.StaleVersion as stale:
        return _stale(request, id, stale.latest)
    except ValueError:
        raise HTTPException(404, "comment not found") from None
    settings = request.app.state.settings
    background.add_task(notify.send, settings, notify.comment_text(report, row, settings.base_url))
    return _thread_response(request, conn, report, user_id, row["parent_id"])


@router.post("/r/{id}/comments/{cid}/resolve")
def resolve(id: str, cid: int, request: Request, conn: Conn, user: User):
    report = _require_report(conn, id)
    if not user[1]:
        return _name_prompt(request, f"/r/{id}", f"#vf_t{cid}")
    root_id = db.resolve_thread(conn, id, cid)
    if root_id is None:
        raise HTTPException(404, "comment not found")
    return _thread_response(request, conn, report, user[0], root_id)


@router.post("/r/{id}/comments/{cid}/reopen")
def reopen(id: str, cid: int, request: Request, conn: Conn, user: User):
    report = _require_report(conn, id)
    if not user[1]:
        return _name_prompt(request, f"/r/{id}", f"#vf_t{cid}")
    if not db.reopen_thread(conn, id, cid):
        raise HTTPException(404, "comment not found")
    return _thread_response(request, conn, report, user[0], cid)


@router.post("/r/{id}/verdict")
def post_verdict(
    id: str,
    request: Request,
    conn: Conn,
    user: User,
    background: BackgroundTasks,
    version: Annotated[int, Form()],
    verdict: Annotated[Literal["approve", "request_changes"], Form()],
    summary: Annotated[str, Form(max_length=2000)] = "",
):
    report = _require_report(conn, id)
    user_id, name = user
    if not name:
        return _name_prompt(request, f"/r/{id}")
    latest = db.latest_version(conn, id)
    if version != latest:
        return _stale(request, id, latest)
    ctx = report_context(conn, report, version, user_id)
    if verdict == "request_changes" and not rules.can_request_changes(
        ctx["threads"], user_id, summary
    ):
        return _partial(request, ["_verdict.html"], ctx | {"error": REFUSED, "summary": summary})
    try:
        row = db.add_verdict(
            conn,
            report_id=id,
            version=version,
            expected_version=version,
            reviewer_id=user_id,
            reviewer_name=name,
            verdict=verdict,
            summary=summary.strip(),
        )
    except db.StaleVersion as stale:
        return _stale(request, id, stale.latest)
    settings = request.app.state.settings
    background.add_task(notify.send, settings, notify.verdict_text(report, row, settings.base_url))
    ctx = report_context(conn, report, version, user_id) | {"oob": True}
    return _partial(request, ["_verdict.html", "_sidebar.html"], ctx)


@router.post("/me")
def set_name(request: Request, name: Annotated[str, Form()], next: Annotated[str, Form()] = "/"):
    if _header_user(request):
        raise HTTPException(403, "identity is set by the proxy")
    name = name.strip()
    if not next.startswith("/") or next.startswith(("//", "/\\")):
        next = "/"
    if not 1 <= len(name) <= 60:
        return RedirectResponse(f"{next}{'&' if '?' in next else '?'}name_error=1", 303)
    response = RedirectResponse(next, status_code=303)
    response.set_cookie(
        "vf_name", url_quote(name), max_age=YEAR, samesite="lax", secure=_secure(request)
    )
    return response
