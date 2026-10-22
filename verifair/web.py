import json
import logging
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

log = logging.getLogger("verifair")
router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _date(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{d.day} {d:%b %Y}"


templates.env.filters["date"] = _date
templates.env.filters["segment"] = lambda value: url_quote(value, safe="")
templates.env.globals["STATUS_LABELS"] = rules.STATUS_LABELS

Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
YEAR = 365 * 24 * 3600
USER_ID = re.compile(r"[A-Za-z0-9_-]{8,16}")
PLAIN_ID = re.compile(r"[^/\\\x00-\x1f\x7f\s]+")
TABS = ("awaiting_review", "changes_requested", "unsigned", "approved")
SCOPES = {"all": "All changes", "author": "My changes", "reviewer": "My reviews"}
REFUSED = "Add a summary, or start a comment thread of your own and leave it open."
UNSIGNED = "The author has not signed this version yet."
SELF_REVIEW = "You signed this report. Someone else has to approve it."
BLOCKED = (
    "Resolve the open blocking threads first. Ask the person who started each one, or an admin,"
    " to resolve it or mark it non-blocking."
)
Body = Annotated[str, Form(max_length=20000)]
Anchor = Annotated[str, Form(max_length=2000)]
LAST_ADMIN = "There must be at least one active admin."
MEMBER_EXISTS = "That member already exists."
MEMBER_INPUT = "Enter an id (1–200 characters) and a name (1–60 characters)."
MEMBER_PLAIN = "Use a plain id such as jane@example.com."
NOT_MEMBER = "You are not a member of this workspace yet. Ask an admin to add {id}."
DEACTIVATED = "Your access to this workspace was turned off. Ask an admin to reactivate {id}."
LABEL_INPUT = "Enter a label (1–60 characters)."
ADMIN_ONLY = "Only an admin can manage members."
OWNER_ONLY = "Only the token owner or an admin can revoke it."
ALREADY_CREATED = (
    "This token was already created. Its secret was shown only once."
    " If you did not copy it, revoke it and create a new one."
)


def plain_id(value: str) -> bool:
    return PLAIN_ID.fullmatch(value) is not None and value.lower() not in (".", "..", "agent")


def _shared(request: Request) -> bool:
    return request.app.state.settings.identity_header is not None


def current_user(request: Request) -> tuple[str, str | None]:
    member = request.state.member
    request.state.user_from_header = _shared(request)
    if request.state.user_from_header:
        if member is None:
            raise HTTPException(401, "sign in required")
        return member["id"], member["name"]
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


def _page(request: Request, user: tuple[str, str | None], name: str, ctx: dict, status=200):
    url = request.url.remove_query_params("name_error")
    ctx = ctx | {
        "user_name": user[1],
        "user_from_header": request.state.user_from_header,
        "here": url.path + (f"?{url.query}" if url.query else ""),
        "name_error": "name_error" in request.query_params,
    }
    response = templates.TemplateResponse(request, name, ctx, status_code=status)
    return _with_id_cookie(request, response)


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


def _status(conn: sqlite3.Connection, id: str, version: int) -> str:
    signature = db.get_signature(conn, id, version)
    return rules.compute_status(
        db.list_verdicts(conn, id),
        version,
        signed=signature is not None,
        signer_id=signature["signer_id"] if signature else None,
    )


def _sign_refusal(request: Request, report: sqlite3.Row, user_id: str) -> str | None:
    owner, author = report["author_id"], report["author"]
    if not request.state.user_from_header:
        return None
    if owner is None:
        return rules.NO_OWNER
    if user_id != owner:
        return f"Only {author if author != 'agent' else owner} can sign this report."
    return None


def _can_moderate(
    shared: bool, member: sqlite3.Row | None, user_id: str, root: sqlite3.Row, report: sqlite3.Row
) -> bool:
    if shared and member is None:
        return False
    return rules.can_moderate(
        shared=shared,
        user_id=user_id,
        role=member["role"] if member is not None else None,
        root_author_id=root["author_id"],
        report_author_id=report["author_id"],
    )


def report_context(
    conn: sqlite3.Connection,
    report: sqlite3.Row,
    version: int,
    user_id: str,
    member: sqlite3.Row | None,
    shared: bool,
) -> dict:
    id = report["id"]
    current = db.latest_version(conn, id)
    shown = db.get_version(conn, id, version)
    sections = split_sections(shown["body"])
    comments = db.list_comments(conn, id)
    verdicts = db.list_verdicts(conn, id)
    threads = build_threads(comments, sections, current)
    open_threads = [t for t in threads if t.root["resolved_at"] is None]
    signature = db.get_signature(conn, id, current)
    resolved_by = {
        t.root["id"]: t.root["resolved_by"] for t in threads if shared and t.root["resolved_by"]
    }
    names = {u["id"]: u["name"] for u in db.list_users(conn)} if resolved_by else {}
    return {
        "report": report,
        "url": f"/r/{url_quote(id)}",
        "ticket": _ticket_label(report["ticket_url"]) if report["ticket_url"] else None,
        "version": version,
        "version_date": shown["created_at"],
        "current_version": current,
        "is_current": version == current,
        "status": rules.compute_status(
            verdicts,
            current,
            signed=signature is not None,
            signer_id=signature["signer_id"] if signature else None,
        ),
        "signature": signature,
        "sections": sections,
        "threads": threads,
        "threads_by_section": threads_by_section(threads),
        "open_count": len(open_threads),
        "blocking_count": sum(t.blocking for t in open_threads),
        "open_by_section": Counter(t.section_slug for t in open_threads),
        "reviewers": rules.reviewers(comments, verdicts),
        "versions": db.list_versions(conn, id)[::-1],
        "links": _links(report),
        "my_verdict": rules.latest_verdicts(verdicts, current).get(user_id),
        "user_id": user_id,
        "can_moderate": {
            t.root["id"] for t in threads if _can_moderate(shared, member, user_id, t.root, report)
        },
        "resolvers": {cid: names.get(by, by) for cid, by in resolved_by.items()},
    }


def _thread_response(
    request, conn, report, user_id, comment_id, reset_form=False, version=None, error=None
):
    shown = version if version is not None else db.latest_version(conn, report["id"])
    ctx = report_context(conn, report, shown, user_id, request.state.member, _shared(request))
    ctx["thread"] = next(
        t for t in ctx["threads"] if comment_id in (t.root["id"], *(r["id"] for r in t.replies))
    )
    ctx["oob"] = True
    ctx["thread_error"] = error
    names = ["_thread.html", "_sidebar.html"]
    section = next((s for s in ctx["sections"] if s.slug == ctx["thread"].section_slug), None)
    if reset_form and section:
        ctx["s"] = section
        names.append("_cform.html")
    return _partial(request, names, ctx)


def _moderation_refusal(request, conn, report, user_id, cid):
    root = db.thread_root(conn, report["id"], cid)
    if root is None:
        raise HTTPException(404, "comment not found")
    if _can_moderate(_shared(request), request.state.member, user_id, root, report):
        return None
    if not request.headers.get("HX-Request"):
        raise HTTPException(403, rules.MODERATION)
    return _thread_response(request, conn, report, user_id, cid, error=rules.MODERATION)


def _landing_scope(scope: str, user_id: str) -> dict:
    if scope not in SCOPES:
        scope = "all"
    titles = {
        "all": "No projects yet",
        "author": "No changes owned by you yet",
        "reviewer": "No review requests for you yet",
    }
    descriptions = {
        "all": "Projects appear here when an agent shares its first change report.",
        "author": "Changes linked to your identity will appear here.",
        "reviewer": "Changes appear here when someone requests your review.",
    }
    return {
        "scope": scope,
        "scopes": SCOPES,
        "identity_id": user_id,
        "scope_empty_title": titles[scope],
        "scope_empty_description": descriptions[scope],
    }


@router.get("/")
def index(request: Request, conn: Conn, user: User, scope: str = "all"):
    scope_context = _landing_scope(scope, user[0])
    scope = scope_context["scope"]
    projects = []
    totals = Counter()
    priority = {status: i for i, status in enumerate(TABS)}
    for project in db.list_projects(conn):
        reports = sorted(
            db.list_reports(
                conn, project, scope=scope, user_id=user[0], legacy_author=not _shared(request)
            ),
            key=lambda r: (r["updated_at"], r["id"]),
            reverse=True,
        )
        if not reports:
            continue
        rows = [
            {"report": r, "status": _status(conn, r["id"], r["latest_version"])} for r in reports
        ]
        statuses = Counter(row["status"] for row in rows)
        totals.update(statuses)
        projects.append(
            {
                "name": project,
                "counts": statuses,
                "total": len(rows),
                "preview": min(rows, key=lambda row: priority[row["status"]], default=None),
                "updated_at": max((r["updated_at"] for r in reports), default=None),
            }
        )
    projects.sort(
        key=lambda p: (
            not p["counts"]["awaiting_review"],
            not (p["counts"]["unsigned"] or p["counts"]["changes_requested"]),
            p["name"].casefold(),
        )
    )
    return _page(
        request,
        user,
        "index.html",
        scope_context
        | {
            "projects": projects,
            "totals": totals,
            "total_changes": sum(totals.values()),
            "tabs": TABS,
        },
    )


@router.get("/p/{project}")
def project_page(
    project: str,
    request: Request,
    conn: Conn,
    user: User,
    tab: str = "all",
    scope: str = "all",
):
    scope_context = _landing_scope(scope, user[0])
    scope = scope_context["scope"]
    if tab not in (*TABS, "all"):
        tab = "all"
    if project not in db.list_projects(conn):
        raise HTTPException(404, "project not found")
    reports = sorted(
        db.list_reports(
            conn, project, scope=scope, user_id=user[0], legacy_author=not _shared(request)
        ),
        key=lambda r: (r["updated_at"], r["id"]),
        reverse=True,
    )
    rows = [
        {
            "report": r,
            "status": _status(conn, r["id"], r["latest_version"]),
            "ticket": _ticket_label(r["ticket_url"]) if r["ticket_url"] else None,
        }
        for r in reports
    ]
    ctx = scope_context | {
        "project": project,
        "tab": tab,
        "tabs": TABS,
        "counts": Counter(row["status"] for row in rows),
        "total": len(rows),
        "rows": [row for row in rows if tab == "all" or row["status"] == tab],
        "empty_title": {
            "all": "No changes yet",
            "awaiting_review": "No changes awaiting review",
            "changes_requested": "No changes requested",
            "unsigned": "No unsigned changes",
            "approved": "No approved changes yet",
        }[tab],
        "empty_description": (
            f"Try another status, or view all {len(reports)} matching "
            f"change{'s' if len(reports) != 1 else ''} in this project."
        ),
        "empty_url": "?tab=all" + (f"&scope={scope}" if scope != "all" else ""),
        "empty_action": "View all changes" if tab != "all" else None,
    }
    if not reports and scope != "all":
        ctx.update(
            empty_title=scope_context["scope_empty_title"],
            empty_description="View everyone's changes in this project, or choose another scope.",
            empty_url="?scope=all&tab=all",
            empty_action="View everyone's changes",
        )
    return _page(request, user, "project.html", ctx)


@router.get("/r/{id}")
def report_page(id: str, request: Request, conn: Conn, user: User):
    report = _require_report(conn, id)
    ctx = report_context(
        conn, report, db.latest_version(conn, id), user[0], request.state.member, _shared(request)
    )
    ctx["sign_refusal"] = _sign_refusal(request, report, user[0])
    return _page(request, user, "report.html", ctx)


@router.get("/r/{id}/v/{n}")
def report_version_page(id: str, n: int, request: Request, conn: Conn, user: User):
    report = _require_report(conn, id)
    if db.get_version(conn, id, n) is None:
        raise HTTPException(404, "version not found")
    ctx = report_context(conn, report, n, user[0], request.state.member, _shared(request))
    return _page(request, user, "report.html", ctx)


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
    prefix: Annotated[str | None, Form(max_length=64)] = None,
    suffix: Annotated[str | None, Form(max_length=64)] = None,
    blocking: Annotated[Literal["0", "1"], Form()] = "1",
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
            blocking=blocking == "1",
            prefix=prefix or None,
            suffix=suffix or None,
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
    root = db.thread_root(conn, id, cid)
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
            reopen=root is not None
            and _can_moderate(_shared(request), request.state.member, user_id, root, report),
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
    refusal = _moderation_refusal(request, conn, report, user[0], cid)
    if refusal:
        return refusal
    root_id = db.resolve_thread(conn, id, cid, by=user[0] if _shared(request) else None)
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


@router.post("/r/{id}/comments/{cid}/blocking")
def set_blocking(
    id: str,
    cid: int,
    request: Request,
    conn: Conn,
    user: User,
    version: Annotated[int, Form()],
    blocking: Annotated[Literal["0", "1"], Form()],
):
    report = _require_report(conn, id)
    if not user[1]:
        return _name_prompt(request, f"/r/{id}", f"#vf_t{cid}")
    refusal = _moderation_refusal(request, conn, report, user[0], cid)
    if refusal:
        return refusal
    try:
        changed = db.set_blocking(conn, id, cid, blocking == "1", expected_version=version)
    except db.StaleVersion as stale:
        return _stale(request, id, stale.latest)
    if not changed:
        raise HTTPException(404, "comment not found")
    return _thread_response(request, conn, report, user[0], cid, version=version)


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

    def validate() -> str | None:
        signature = db.get_signature(conn, id, version)
        if signature is None:
            return UNSIGNED
        if signature["signer_id"] == user_id:
            return SELF_REVIEW
        threads = build_threads(db.list_comments(conn, id), [], version)
        if verdict == "approve" and not rules.can_approve(threads):
            return BLOCKED
        if verdict == "request_changes" and not rules.can_request_changes(
            threads, user_id, summary
        ):
            return REFUSED
        return None

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
            validate=validate,
        )
    except db.StaleVersion as stale:
        return _stale(request, id, stale.latest)
    except db.VerdictRefused as refused:
        ctx = report_context(conn, report, version, user_id, request.state.member, _shared(request))
        return _partial(
            request, ["_verdict.html"], ctx | {"error": str(refused), "summary": summary}
        )
    settings = request.app.state.settings
    background.add_task(notify.send, settings, notify.verdict_text(report, row, settings.base_url))
    ctx = report_context(conn, report, version, user_id, request.state.member, _shared(request))
    return _partial(request, ["_verdict.html", "_sidebar.html"], ctx | {"oob": True})


@router.post("/r/{id}/sign")
def sign(
    id: str,
    request: Request,
    conn: Conn,
    user: User,
    background: BackgroundTasks,
    version: Annotated[int, Form()],
):
    report = _require_report(conn, id)
    user_id, name = user
    if not name:
        return _name_prompt(request, f"/r/{id}")
    refusal = _sign_refusal(request, report, user_id)
    if refusal:
        raise HTTPException(403, refusal)
    try:
        db.sign_version(conn, report_id=id, version=version, signer_id=user_id, signer_name=name)
    except db.StaleVersion as stale:
        return _stale(request, id, stale.latest)
    except db.AlreadySigned:
        pass
    else:
        settings = request.app.state.settings
        text = notify.ready_for_review_text(report, version, name, settings.base_url)
        background.add_task(notify.send, settings, text)
    ctx = report_context(conn, report, version, user_id, request.state.member, _shared(request))
    ctx |= {"oob": True, "verdict_oob": True}
    return _partial(request, ["_header.html", "_sidebar.html", "_verdict.html"], ctx)


@router.post("/me")
def set_name(request: Request, name: Annotated[str, Form()], next: Annotated[str, Form()] = "/"):
    if _shared(request):
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


def _access(
    request: Request,
    conn: sqlite3.Connection,
    user,
    *,
    error=None,
    token=None,
    token_note=None,
    member_error=None,
    member_form=None,
    status=200,
):
    member = request.state.member
    ctx = {
        "shared": _shared(request),
        "error": error,
        "new_token": token,
        "token_note": token_note,
        "member_error": member_error,
        "member_form": member_form or {"id": "", "name": "", "role": "member"},
        "roles": db.ROLES,
        "nonce": secrets.token_urlsafe(16),
    }
    if member is not None:
        ctx |= {
            "me": member["id"],
            "tokens": db.list_tokens(conn, member["id"]),
            "is_admin": member["role"] == "admin",
        }
        if ctx["is_admin"]:
            ctx |= {"users": db.list_users(conn), "all_tokens": db.list_tokens(conn)}
    return _page(request, user, "access.html", ctx, status)


def _denied(request: Request, message: str, back: str | None = "/access"):
    ctx = {"message": message, "back": back}
    return templates.TemplateResponse(request, "denied.html", ctx, status_code=403)


def access_member(request: Request) -> sqlite3.Row:
    if not _shared(request):
        raise HTTPException(404, "not found")
    return request.state.member


Member = Annotated[sqlite3.Row, Depends(access_member)]


@router.get("/access")
def access_page(request: Request, conn: Conn, user: User):
    return _access(request, conn, user)


@router.post("/access/users")
def add_member(
    request: Request,
    conn: Conn,
    user: User,
    member: Member,
    id: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    role: Annotated[Literal["admin", "member"], Form()] = "member",
):
    if member["role"] != "admin":
        return _denied(request, ADMIN_ONLY)

    def refuse(error: str, status: int):
        form = {"id": id, "name": name, "role": role}
        return _access(request, conn, user, member_error=error, member_form=form, status=status)

    clean_id, clean_name = id.strip().lower(), name.strip()
    if not (1 <= len(clean_id) <= 200 and 1 <= len(clean_name) <= 60):
        return refuse(MEMBER_INPUT, 422)
    if not plain_id(clean_id):
        return refuse(MEMBER_PLAIN, 422)
    try:
        db.add_user(conn, id=clean_id, name=clean_name, role=role)
    except ValueError:
        return refuse(MEMBER_EXISTS, 409)
    log.info("access action=add_user actor=%s target=%s role=%s", user[0], clean_id, role)
    return RedirectResponse("/access", 303)


@router.post("/access/users/{id:path}")
def update_member(
    id: str,
    request: Request,
    conn: Conn,
    user: User,
    member: Member,
    role: Annotated[Literal["admin", "member"] | None, Form()] = None,
    active: Annotated[Literal["0", "1"] | None, Form()] = None,
):
    if member["role"] != "admin":
        return _denied(request, ADMIN_ONLY)
    open_tokens = [t for t in db.list_tokens(conn, id) if t["revoked_at"] is None]
    try:
        row = db.update_user(conn, id, role=role, active=None if active is None else active == "1")
    except db.LastAdmin:
        return _access(request, conn, user, error=LAST_ADMIN, status=409)
    except ValueError:
        raise HTTPException(404, "member not found") from None
    log.info(
        "access action=update_user actor=%s target=%s role=%s active=%s tokens_revoked=%d",
        user[0],
        id,
        row["role"],
        row["active"],
        0 if row["active"] else len(open_tokens),
    )
    return RedirectResponse("/access", 303)


@router.post("/access/tokens")
def new_token(
    request: Request,
    conn: Conn,
    user: User,
    member: Member,
    label: Annotated[str, Form()] = "",
    nonce: Annotated[str, Form(max_length=64)] = "",
):
    label = label.strip()
    if not 1 <= len(label) <= 60:
        return _access(request, conn, user, error=LABEL_INPUT, status=422)
    try:
        token, row = db.create_token(conn, owner_id=member["id"], label=label, nonce=nonce or None)
    except db.AlreadyCreated:
        return _access(request, conn, user, token_note=ALREADY_CREATED)
    except ValueError:
        return _denied(request, DEACTIVATED.format(id=member["id"]), back=None)
    log.info("access action=create_token actor=%s target=%s", user[0], row["id"])
    return _access(request, conn, user, token=token)


@router.post("/access/tokens/{token_id}/revoke")
def revoke_token(token_id: str, request: Request, conn: Conn, user: User, member: Member):
    token = db.get_token(conn, token_id)
    if token is None:
        raise HTTPException(404, "token not found")
    if token["owner_id"] != member["id"] and member["role"] != "admin":
        return _denied(request, OWNER_ONLY)
    if db.revoke_token(conn, token_id):
        log.info("access action=revoke_token actor=%s target=%s", user[0], token_id)
    return RedirectResponse("/access", 303)
