import importlib.metadata
import json
import logging
import re
import secrets
import threading
import zlib
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote as url_quote
from urllib.parse import unquote

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from pydantic import AfterValidator

from verifair import db, notify, rules
from verifair.deps import get_conn
from verifair.render import (
    build_threads,
    normalize,
    quote_found,
    render_markdown,
    section_plain_text,
    split_sections,
    threads_by_section,
)
from verifair.traces import execution_tree

log = logging.getLogger("verifair")
router = APIRouter()
_MAIL_LOCK = threading.Lock()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _date(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{d.day} {d:%b %Y}"


def _elapsed(iso: str, now: datetime | None) -> timedelta:
    return (now or datetime.now(UTC)) - datetime.fromisoformat(iso)


def age(iso: str, now: datetime | None = None) -> str:
    seconds = _elapsed(iso, now).total_seconds()
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def ago(iso: str, now: datetime | None = None) -> str:
    if _elapsed(iso, now) < timedelta(minutes=1):
        return "just now"
    if _elapsed(iso, now) < timedelta(days=30):
        return f"{age(iso, now)} ago"
    return _date(iso)


def avatar(id: str) -> str:
    return f"c{zlib.crc32(id.encode()) % 5}"


templates.env.filters["date"] = _date
templates.env.filters |= {"age": age, "ago": ago, "avatar": avatar}
templates.env.filters["segment"] = lambda value: url_quote(value, safe="")
VERSION = importlib.metadata.version("verifair")


def icon(name: str, cls: str = "ic") -> Markup:
    return Markup(
        f'<svg class="{escape(cls)}" aria-hidden="true">'
        f'<use href="/static/icons.svg#{escape(name)}"/></svg>'
    )


OPEN_STATUSES = ("unsigned", "awaiting_review", "changes_requested", "awaiting_implementation")
TABS = (
    "open",
    "awaiting_review",
    "changes_requested",
    "unsigned",
    "awaiting_implementation",
    "approved",
    "all",
)
TAB_LABELS = {"open": "Open", **rules.STATUS_LABELS, "all": "All"}
STATUS_ICONS = {
    "unsigned": "circle-dashed",
    "awaiting_review": "circle-dot",
    "changes_requested": "circle-x",
    "approved": "circle-check",
    "awaiting_implementation": "circle-dot",
}
templates.env.globals |= {
    "STATUS_LABELS": rules.STATUS_LABELS,
    "STATUS_ICONS": STATUS_ICONS,
    "TABS": TABS,
    "TAB_LABELS": TAB_LABELS,
    "VERSION": VERSION,
    "icon": icon,
}

Conn = Annotated[db.Connection, Depends(get_conn, scope="function")]
YEAR = 365 * 24 * 3600
USER_ID = re.compile(r"[A-Za-z0-9_-]{8,16}")
PLAIN_ID = re.compile(r"[^/\\\x00-\x1f\x7f\s]+")
SCOPES = {"all": "All changes", "author": "My changes", "reviewer": "My reviews"}
REFUSED = "Add a summary, or start a comment thread of your own and leave it open."
UNSIGNED = "The author has not signed this version yet."
SELF_REVIEW = "You signed this report. Someone else has to approve it."
BLOCKED_LOCAL = "Resolve the open blocking threads first, or mark them non-blocking."
BLOCKED_OWN = "Resolve your open blocking threads first, or mark them non-blocking."
BLOCKED = (
    "Resolve the open blocking threads first. Ask the person who started each one, the report"
    " owner for agent threads, or an admin to resolve it or mark it non-blocking."
)
NoNul = AfterValidator(db.no_nul)
Body = Annotated[str, Form(max_length=20000), NoNul]
LAST_ADMIN = "There must be at least one active admin."
MEMBER_EXISTS = "That member already exists."
MEMBER_INPUT = "Enter an id (1–200 characters) and a name (1–60 characters)."
MEMBER_PLAIN = "Use a plain id such as jane@example.com."
MEMBER_AGENT = "The id agent is reserved for agents."
NOT_MEMBER = "You are not a member of this workspace yet. Ask an admin to add {id}."
DEACTIVATED = "Your access to this workspace was turned off. Ask an admin to reactivate {id}."
LABEL_INPUT = "Enter a label (1–60 characters)."
ADMIN_ONLY = "Only an admin can manage members."
REVIEWER_REFUSED = "Pick an active member who does not own this report."
REVIEWER_KEPT = "{name} already gave a verdict on this version, so the request stays."
OWNER_ONLY = "Only the token owner or an admin can revoke it."
ALREADY_CREATED = (
    "This token was already created. Its secret was shown only once."
    " If you did not copy it, revoke it and create a new one."
)


def _fits(value: str, longest: int) -> bool:
    return 1 <= len(value) <= longest and db.storable(value)


def plain_id(value: str) -> bool:
    return PLAIN_ID.fullmatch(value) is not None and value.lower() not in (".", "..", "agent")


def _shared(request: Request) -> bool:
    return request.app.state.settings.identity_header is not None


async def current_user(request: Request) -> tuple[str, str | None]:
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
    name = unquote(request.cookies.get("vf_name", ""))
    return user_id, name if name and db.storable(name) else None


User = Annotated[tuple[str, str | None], Depends(current_user)]


async def access_member(request: Request) -> db.Row:
    if not _shared(request):
        raise HTTPException(404, "not found")
    return request.state.member


Member = Annotated[db.Row, Depends(access_member)]


def _secure(request: Request) -> bool:
    return request.app.state.settings.base_url.startswith("https")


def _with_id_cookie(request: Request, response):
    new_id = getattr(request.state, "new_vf_id", None)
    if new_id:
        response.set_cookie(
            "vf_id", new_id, max_age=YEAR, samesite="lax", httponly=True, secure=_secure(request)
        )
    return response


NAV_PROJECTS_SHOWN = 8


def _in_scope(row: dict, scope: str, user_id: str, legacy: bool) -> bool:
    report = row["report"]
    if scope == "author":
        return report["author_id"] == user_id or (
            legacy and report["author_id"] is None and report["author"] == user_id
        )
    if scope == "reviewer":
        return user_id in row["requested"]
    return True


def _work(request: Request, conn: db.Connection, user_id: str) -> dict:
    legacy = not _shared(request)
    rows = _change_rows(conn, db.list_reports(conn))
    mine = {row["report"]["id"] for row in rows if _in_scope(row, "author", user_id, legacy)}
    requested = {row["report"]["id"] for row in rows if user_id in row["requested"]}
    if legacy:
        # in local mode anyone may sign an unowned draft, and signing makes them its owner,
        # but a draft someone asked you to review is not yours to sign
        mine |= {
            row["report"]["id"]
            for row in rows
            if row["report"]["author_id"] is None
            and row["status"] == "unsigned"
            and row["report"]["id"] not in requested
        }
    groups = rules.inbox(rows, user_id=user_id, mine=mine, requested=requested)
    return {"rows": rows, "groups": groups}


def _need_count(groups: dict) -> int:
    return sum(len(groups[key]) for key in ("review", "sign", "returned", "implementation"))


def _nav(work: dict) -> dict:
    rows = work["rows"]
    awaiting = Counter(r["report"]["project"] for r in rows if r["status"] == "awaiting_review")
    projects = [
        {"name": p, "awaiting": awaiting[p]}
        for p in sorted({r["report"]["project"] for r in rows}, key=str.casefold)
    ]
    return {
        "nav_projects": projects[:NAV_PROJECTS_SHOWN],
        "nav_rest": projects[NAV_PROJECTS_SHOWN:],
        "inbox_count": _need_count(work["groups"]),
    }


def _shell(request: Request, conn: db.Connection, user: tuple[str, str | None], work=None) -> dict:
    url = request.url.remove_query_params("name_error")
    member = request.state.member
    return _nav(work or _work(request, conn, user[0])) | {
        "user_id": user[0],
        "user_name": user[1],
        "user_from_header": request.state.user_from_header,
        "shared": _shared(request),
        "member": member,
        "is_admin": member is not None and member["role"] == "admin",
        "base_url": request.app.state.settings.base_url,
        "here": url.path + (f"?{url.query}" if url.query else ""),
        "name_error": "name_error" in request.query_params,
    }


def _page(
    request: Request,
    conn: db.Connection,
    user: tuple[str, str | None],
    name: str,
    ctx: dict,
    status=200,
    work: dict | None = None,
):
    ctx = ctx | _shell(request, conn, user, work)
    response = templates.TemplateResponse(request, name, ctx, status_code=status)
    return _with_id_cookie(request, response)


def _nav_oob(request: Request, conn: db.Connection, user, ctx: dict) -> str:
    nav = _shell(request, conn, user) | {
        "nav_oob": True,
        "here": ctx["selected_url"],
        "active_project": ctx["report"]["project"],
    }
    return templates.get_template("_nav.html").render(nav)


def _partial(
    request: Request, names: list[str], ctx: dict, headers: dict | None = None, tail: str = ""
):
    html = "".join(templates.get_template(n).render(ctx) for n in names) + tail
    return _with_id_cookie(request, HTMLResponse(html, headers=headers))


def _retarget(request: Request, target: str | None = None) -> dict | None:
    trigger = request.headers.get("HX-Trigger")
    target = target or (f"#{trigger}" if trigger else None)
    return {"HX-Retarget": target, "HX-Reswap": "outerHTML"} if target else None


def _name_prompt(request: Request, next: str, target: str | None = None):
    return _partial(request, ["_name_prompt.html"], {"next": next}, _retarget(request, target))


def _stale(request: Request, report: db.Row, latest: int, target: db.Target):
    ctx = {"url": _document_url(report, target), "latest": latest}
    return _partial(request, ["_stale.html"], ctx, _retarget(request))


def _require_body(body: str) -> str:
    body = body.strip()
    if not body:
        raise HTTPException(422, "comment body is empty")
    return body


def _require_report(conn: db.Connection, id: str) -> db.Row:
    report = db.get_report(conn, id)
    if report is None:
        raise HTTPException(404, "report not found")
    return report


def _ticket_label(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _links(report: db.Row) -> list[tuple[str, str]]:
    links = [(link["label"], link["url"]) for link in json.loads(report["links"])]
    if report["ticket_url"]:
        links.insert(0, (_ticket_label(report["ticket_url"]), report["ticket_url"]))
    return links


def _sign_refusal(request: Request, report: db.Row, user_id: str) -> str | None:
    owner, author = report["author_id"], report["author"]
    if not request.state.user_from_header:
        return None
    if owner is None:
        return rules.NO_OWNER
    if user_id != owner:
        return f"Only {author if author != 'agent' else owner} can sign this report."
    return None


def _can_moderate(
    shared: bool, member: db.Row | None, user_id: str, root: db.Row, report: db.Row
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


@contextmanager
def _read(conn: db.Connection):
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield


def _document_url(report: db.Row, target: db.Target, snapshot: int | None = None) -> str:
    url = f"/r/{url_quote(report['id'])}"
    if snapshot is not None:
        url += f"/v/{snapshot}"
    if target == "plan" or report["workflow"] == "plan_first":
        url += f"?tab={target}"
    return url


def _target_label(report, target, revision, *, capital=False):
    if target == "plan":
        label = f"plan revision {revision}"
    elif report["workflow"] == "plan_first":
        label = f"implementation v{revision}"
    else:
        label = f"v{revision}"
    return label[0].upper() + label[1:] if capital else label


def report_context(
    conn: db.Connection,
    report: db.Row,
    version: int,
    user_id: str,
    member: db.Row | None,
    shared: bool,
    *,
    target: db.Target = "implementation",
) -> dict:
    id = report["id"]
    current = db.latest_version(conn, id)
    shown = db.get_version(conn, id, version)
    latest = shown if version == current else db.get_version(conn, id, current)
    plan_first = report["workflow"] == "plan_first"
    readonly_plan = target == "plan" and not plan_first
    feedback_target = "implementation" if readonly_plan else target
    revision = shown["plan_revision"] if feedback_target == "plan" else version
    markdown = shown[feedback_target if feedback_target == "plan" else "body"]
    sections = split_sections(markdown) if markdown is not None else []
    comments = [
        c for c in db.list_comments(conn, id, target=feedback_target) if c["version"] <= revision
    ]
    verdicts = [
        v for v in db.list_verdicts(conn, id, target=feedback_target) if v["version"] <= revision
    ]
    threads = build_threads(comments, sections, revision)
    open_threads = [t for t in threads if t.root["resolved_at"] is None]
    signature = (
        db.get_signature(conn, id, revision, target=feedback_target)
        if markdown is not None
        else None
    )
    plan = db.plan_review_state(conn, id)
    shown_plan = plan if version == current else db.plan_review_state(conn, id, version)
    implementation_verdicts = db.list_verdicts(conn, id)
    implementation_signature = (
        db.get_signature(conn, id, current) if latest["body"] is not None else None
    )
    implementation_status = (
        rules.compute_status(
            implementation_verdicts,
            current,
            signed=implementation_signature is not None,
            signer_id=implementation_signature["signer_id"] if implementation_signature else None,
        )
        if latest["body"] is not None
        else None
    )
    stage, status = rules.review_stage(
        workflow=report["workflow"],
        has_body=latest["body"] is not None,
        plan_status=plan["status"] if plan else None,
        implementation_status=implementation_status,
    )
    selected_status = rules.compute_status(
        verdicts,
        revision,
        signed=signature is not None,
        signer_id=signature["signer_id"] if signature else None,
    )
    if feedback_target == "plan" and shown_plan:
        selected_status = shown_plan["status"]
    gate = plan_first and feedback_target == "implementation" and plan["status"] != "approved"
    resolved_by = {
        t.root["id"]: t.root["resolved_by"] for t in threads if shared and t.root["resolved_by"]
    }
    signer_id = signature["signer_id"] if signature else None
    all_requested = db.requested_reviewers(conn, id)
    requested = [p for p in all_requested if p != signer_id]
    users = db.list_users(conn) if shared or resolved_by or requested else []
    names = {u["id"]: u["name"] for u in users}
    judged = rules.latest_verdicts(verdicts, revision)
    can_change = version == current and not readonly_plan
    protected = {
        v["reviewer_id"]
        for v in implementation_verdicts
        if latest["body"] is not None and v["version"] == current
    }
    if plan:
        protected.update(plan["latest_verdicts"])
    reviewers = rules.reviewers(
        comments, verdicts, {person: names.get(person, person) for person in requested}
    )
    for reviewer in reviewers:
        if reviewer.verdict_version is not None:
            reviewer.label = reviewer.label.replace(
                f"v{reviewer.verdict_version}",
                _target_label(report, feedback_target, reviewer.verdict_version),
            )
    url = f"/r/{url_quote(id)}"
    reviewable = markdown is not None and not readonly_plan
    return {
        "report": report,
        "url": url,
        "shown_url": url if version == current else f"{url}/v/{version}",
        "selected_url": _document_url(report, target),
        "selection_query": "?tab=plan"
        if target == "plan"
        else "?tab=implementation"
        if plan_first
        else "",
        "target": target,
        "revision": revision,
        "stage": stage,
        "target_status": selected_status,
        "target_label": _target_label(report, feedback_target, revision, capital=True),
        "target_phrase": _target_label(report, feedback_target, revision),
        "view_tab": "plan" if target == "plan" else "implementation" if plan_first else "report",
        "plan_first": plan_first,
        "reviewable": reviewable,
        "plan_gate": gate,
        "has_body": shown["body"] is not None,
        "plan_html": render_markdown(shown["plan"]) if shown["plan"] is not None else None,
        "ticket": _ticket_label(report["ticket_url"]) if report["ticket_url"] else None,
        "version": version,
        "version_date": shown["created_at"],
        "current_version": current,
        "is_current": version == current,
        "status": status,
        "signature": signature,
        "sections": sections,
        "threads": threads,
        "threads_by_section": threads_by_section(threads),
        "open_count": len(open_threads),
        "blocking_count": sum(t.blocking for t in open_threads),
        "open_by_section": Counter(t.section_slug for t in open_threads),
        "reviewers": reviewers,
        "verdict_history": [
            {
                **v,
                "label": _target_label(report, feedback_target, v["version"]),
                "url": _document_url(report, target, v["version"]),
            }
            for v in verdicts
        ],
        "versions": db.list_versions(conn, id)[::-1],
        "links": _links(report),
        "my_verdict": judged.get(user_id),
        "verdict_block": "The current plan must be approved before reviewing the implementation."
        if gate
        else UNSIGNED
        if signature is None
        else SELF_REVIEW
        if signer_id == user_id
        else None,
        "user_id": user_id,
        "can_moderate": {
            t.root["id"] for t in threads if _can_moderate(shared, member, user_id, t.root, report)
        },
        "resolvers": {cid: names.get(by, by) for cid, by in resolved_by.items()},
        "labels": _labels(users),
        "removable": {p for p in requested if p not in protected} if can_change else set(),
        "request_options": _request_options(users, report, all_requested)
        if shared and can_change
        else [],
        "requests_note": not can_change and (shared or bool(all_requested)),
    }


def _labels(users: list[db.Row]) -> dict[str, str]:
    names = Counter(u["name"].lower() for u in users)
    return {
        u["id"]: u["name"] if names[u["name"].lower()] == 1 else f"{u['name']} ({u['id']})"
        for u in users
    }


def _request_options(users: list[db.Row], report: db.Row, requested: list[str]) -> list[dict]:
    active = sorted((u for u in users if u["active"]), key=lambda u: (u["name"].lower(), u["id"]))
    labels = _labels(users)
    return [
        {"id": u["id"], "label": labels[u["id"]]}
        for u in active
        if u["id"] != report["author_id"] and u["id"] not in requested
    ]


def _response_context(request, conn, id, user, target, version=None):
    report = _require_report(conn, id)
    shown = db.latest_version(conn, id) if version is None else version
    ctx = report_context(
        conn, report, shown, user[0], request.state.member, _shared(request), target=target
    )
    ctx |= {"sign_refusal": _sign_refusal(request, report, user[0]), "user_name": user[1]}
    return ctx


def _thread_response(
    request, conn, report, user, comment_id, reset_form=False, version=None, error=None
):
    with _read(conn):
        root = db.thread_root(conn, report["id"], comment_id)
        if root is None:
            raise HTTPException(404, "comment not found")
        current = db.latest_version(conn, report["id"])
        if version is None:
            return _partial(
                request,
                ["_stale.html"],
                {
                    "url": _document_url(report, root["target"]),
                    "context_missing": True,
                    "action_error": error,
                },
                _retarget(request),
            )
        if version != current:
            return _stale(request, report, current, root["target"])
        ctx = _response_context(request, conn, report["id"], user, root["target"], version)
        ctx["thread"] = next(t for t in ctx["threads"] if root["id"] == t.root["id"])
        ctx |= {"oob": True, "header_oob": True, "verdict_oob": True, "thread_error": error}
        names = ["_thread.html", "_sidebar.html", "_header.html"]
        if report["workflow"] == "plan_first":
            names.append("_verdict.html")
        section = next((s for s in ctx["sections"] if s.slug == ctx["thread"].section_slug), None)
        if reset_form and (section or root["section"] is None):
            ctx["s"] = section or {"slug": "vf_general", "heading": None}
            names.append("_cform.html")
        return _partial(request, names, ctx, tail=_nav_oob(request, conn, user, ctx))


def _moderation_refusal(request, conn, report, user, cid, *, version=None):
    root = db.thread_root(conn, report["id"], cid)
    if root is None:
        raise HTTPException(404, "comment not found")
    if _can_moderate(_shared(request), request.state.member, user[0], root, report):
        return None
    if not request.headers.get("HX-Request"):
        raise HTTPException(403, rules.MODERATION)
    return _thread_response(
        request, conn, report, user, cid, version=version, error=rules.MODERATION
    )


def _scope_empty(scope: str) -> dict:
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
    return {"empty_title": titles[scope], "empty_description": descriptions[scope]}


@router.get("/")
def index(request: Request, conn: Conn, user: User, scope: str | None = None):
    if scope is not None:
        return RedirectResponse(f"/changes?scope={scope}" if scope in SCOPES else "/changes", 303)
    with _read(conn):
        work = _work(request, conn, user[0])
        groups = work["groups"]
        ctx = {"active": "inbox", "first_run": not work["rows"]}
        if work["rows"]:
            ctx |= {"groups": groups, "need_count": _need_count(groups)}
        return _page(request, conn, user, "inbox.html", ctx, work=work)


def _change_rows(conn: db.Connection, reports: list[db.Row]) -> list[dict]:
    ids = [report["id"] for report in reports]
    verdicts_by, requests_by = db.verdicts_by_report(conn, ids), db.requests_by_report(conn, ids)
    names = {u["id"]: u["name"] for u in db.list_users(conn)}
    rows = []
    for report in reports:
        report = dict(report)
        id, version = report["id"], report["latest_version"]
        targets = {
            target: [v for v in verdicts_by[id] if v["target"] == target]
            for target in ("plan", "implementation")
        }
        statuses = {
            target: rules.compute_status(
                targets[target],
                report["plan_revision"] if target == "plan" else version,
                signed=report[f"{target}_signed_at"] is not None,
                signer_id=report[f"{target}_signer_id"],
            )
            for target in targets
        }
        plan_status = rules.plan_status(statuses["plan"], report["open_plan_blocking"])
        stage, status = rules.review_stage(
            workflow=report["workflow"],
            has_body=report["has_body"],
            plan_status=plan_status,
            implementation_status=statuses["implementation"] if report["has_body"] else None,
        )
        target = "implementation" if stage == "implementation" else "plan"
        revision = version if target == "implementation" else report["plan_revision"]
        verdicts, requested = targets[target], requests_by[id]
        latest = rules.latest_verdicts(verdicts, revision)
        report["signer_id"] = report[f"{target}_signer_id"]
        report["signed_at"] = report[f"{target}_signed_at"]
        report["open_threads"] = (
            report["open_plan_threads"] if target == "plan" else report["open_threads"]
        )
        verdict_names = {v["reviewer_id"]: v["reviewer_name"] for v in verdicts}
        people = [*requested, *(v["reviewer_id"] for v in verdicts)]
        reviewers = []
        for person in dict.fromkeys(people):
            mark = (
                latest[person]["verdict"]
                if person in latest
                else "stale"
                if person in verdict_names
                else None
            )
            reviewers.append(
                {
                    "id": person,
                    "name": verdict_names.get(person) or names.get(person) or person,
                    "mark": mark,
                }
            )
        rows.append(
            {
                "report": report,
                "status": status,
                "stage": stage,
                "target": target,
                "revision": revision,
                "url": _document_url(report, target),
                "target_phrase": _target_label(report, target, revision),
                "ticket": _ticket_label(report["ticket_url"]) if report["ticket_url"] else None,
                "signed_at": report["signed_at"],
                "reviewed_by": set(latest),
                "requested": set(requested),
                "reviewers": reviewers,
            }
        )
    return rows


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _list_page(
    request: Request,
    conn: db.Connection,
    user: tuple[str, str | None],
    *,
    scope: str,
    tab: str,
    project: str | None = None,
):
    with _read(conn):
        if scope not in SCOPES:
            scope = "all"
        if tab not in TABS:
            tab = "open"
        work = _work(request, conn, user[0])
        legacy = not _shared(request)
        rows = sorted(
            (
                row
                for row in work["rows"]
                if project in (None, row["report"]["project"])
                and _in_scope(row, scope, user[0], legacy)
            ),
            key=lambda row: (row["report"]["updated_at"], row["report"]["id"]),
            reverse=True,
        )
        reports = [row["report"] for row in rows]
        by_status = Counter(row["status"] for row in rows)
        counts = {status: by_status[status] for status in rules.STATUS_LABELS} | {
            "open": sum(by_status[status] for status in OPEN_STATUSES),
            "all": len(rows),
        }
        awaiting = counts["awaiting_review"]
        if project is None:
            projects = len({r["project"] for r in reports})
            meta = (
                f"{_plural(len(rows), 'change')} in {_plural(projects, 'project')}"
                f" · {awaiting} awaiting review"
            )
        else:
            meta = f"{_plural(len(rows), 'change')} · {awaiting} awaiting review"
            if reports:
                meta += f" · updated {ago(max(r['updated_at'] for r in reports))}"
        if tab == "all":
            shown = rows
        elif tab == "open":
            shown = [row for row in rows if row["status"] in OPEN_STATUSES]
        else:
            shown = [row for row in rows if row["status"] == tab]
        where = " in this project" if project else ""
        ctx = {
            "title": project or SCOPES[scope],
            "meta": meta,
            "project": project,
            "active": scope if project is None else None,
            "active_project": project,
            "scope": scope,
            "tab": tab,
            "counts": counts,
            "rows": shown,
            "empty_title": {
                "open": "No open changes",
                "all": "No changes yet",
                "awaiting_review": "No changes awaiting review",
                "changes_requested": "No changes requested",
                "unsigned": "No unsigned changes",
                "approved": "No approved changes yet",
                "awaiting_implementation": "No changes awaiting implementation",
            }[tab],
            "empty_description": (
                f"Try another status, or view all {len(reports)} matching "
                f"change{'s' if len(reports) != 1 else ''}{where}."
            ),
            "empty_url": "?tab=all" + (f"&scope={scope}" if scope != "all" else ""),
            "empty_action": "View all changes" if tab != "all" else None,
        }
        if not reports:
            ctx.update(_scope_empty(scope), empty_url="?scope=all&tab=all", empty_action=None)
            if scope != "all":
                ctx.update(
                    empty_description=f"View everyone's changes{where}.",
                    empty_action="View everyone's changes",
                )
        return _page(request, conn, user, "changes.html", ctx, work=work)


@router.get("/changes")
def changes(request: Request, conn: Conn, user: User, scope: str = "all", tab: str = "open"):
    return _list_page(request, conn, user, scope=scope, tab=tab)


@router.get("/p/{project}")
def project_page(
    project: str,
    request: Request,
    conn: Conn,
    user: User,
    tab: str = "open",
    scope: str = "all",
):
    if not db.project_exists(conn, project):
        raise HTTPException(404, "project not found")
    return _list_page(request, conn, user, scope=scope, tab=tab, project=project)


def _selected_target(conn, report, version, tab):
    if tab in ("report", "implementation"):
        return "implementation"
    if tab == "plan" and db.get_version(conn, report["id"], version)["plan"] is not None:
        return "plan"
    if report["workflow"] == "plan_first":
        plan = db.plan_review_state(conn, report["id"], version)
        shown = db.get_version(conn, report["id"], version)
        return "plan" if plan["status"] != "approved" or shown["body"] is None else "implementation"
    return "implementation"


def _report_page(id, request, conn, user, tab, version=None):
    with _read(conn):
        report = _require_report(conn, id)
        version = db.latest_version(conn, id) if version is None else version
        if db.get_version(conn, id, version) is None:
            raise HTTPException(404, "version not found")
        target = (
            "implementation" if tab == "execution" else _selected_target(conn, report, version, tab)
        )
        ctx = _response_context(request, conn, id, user, target, version)
        ctx["has_trace"] = next(v["has_trace"] for v in ctx["versions"] if v["version"] == version)
        if tab == "execution":
            trace = db.get_trace(conn, id, version)
            bound_plan = (
                db.get_version(conn, id, trace.binding.plan_revision)
                if trace is not None and trace.binding.plan_revision is not None
                else None
            )
            ctx |= {
                "view_tab": "execution",
                "selection_query": "?tab=execution",
                "selected_url": f"{ctx['url']}?tab=execution",
                "reviewable": False,
                "trace": trace,
                "execution_tree": execution_tree(trace.document) if trace else [],
                "bound_plan_html": render_markdown(bound_plan["plan"]) if bound_plan else None,
                "current_plan_revision": ctx["versions"][0]["plan_revision"],
                "review_url": _document_url(
                    report, "implementation", None if ctx["is_current"] else version
                ),
            }
        ctx["active_project"] = report["project"]
        return _page(request, conn, user, "report.html", ctx)


@router.get("/r/{id}")
def report_page(id: str, request: Request, conn: Conn, user: User, tab: str | None = None):
    return _report_page(id, request, conn, user, tab)


@router.get("/r/{id}/v/{n}")
def report_version_page(
    id: str, n: int, request: Request, conn: Conn, user: User, tab: str | None = None
):
    return _report_page(id, request, conn, user, tab, n)


def _revision(target, revision, version):
    if target == "plan" and revision is None:
        raise HTTPException(422, "plan revision is required")
    return version if revision is None else revision


def _action_error(request, conn, report, target, error):
    if isinstance(error, db.StaleVersion):
        return _stale(request, report, error.latest, target)
    if isinstance(error, db.StaleRevision):
        return _partial(
            request,
            ["_stale.html"],
            {
                "url": _document_url(report, target),
                "document_changed": True,
                "latest": db.latest_version(conn, report["id"]),
            },
            _retarget(request),
        )
    if isinstance(error, (db.PlanNotApproved, db.MissingDocument)):
        return _partial(
            request,
            ["_stale.html"],
            {
                "url": _document_url(report, target),
                "action_error": str(error),
            },
            _retarget(request),
        )
    raise HTTPException(422, str(error)) from error


def _action_response(
    request, conn, id, user, target, *, version, signing=False, error=None, summary=""
):
    with _read(conn):
        ctx = _response_context(request, conn, id, user, target, version)
        if not ctx["is_current"]:
            return _stale(request, ctx["report"], ctx["current_version"], target)
        ctx |= {
            "oob": True,
            "header_oob": not signing,
            "verdict_oob": signing,
            "error": error,
            "summary": summary,
        }
        names = (
            ["_header.html", "_sidebar.html", "_verdict.html"]
            if signing
            else ["_verdict.html", "_header.html", "_sidebar.html"]
        )
        return _partial(request, names, ctx, tail=_nav_oob(request, conn, user, ctx))


@router.post("/r/{id}/comments")
def post_comment(
    id: str,
    request: Request,
    conn: Conn,
    user: User,
    background: BackgroundTasks,
    version: Annotated[int, Form()],
    body: Body,
    section: Annotated[str | None, Form(max_length=2000), NoNul] = None,
    quote: Annotated[str | None, Form(max_length=2000), NoNul] = None,
    prefix: Annotated[str | None, Form(max_length=64), NoNul] = None,
    suffix: Annotated[str | None, Form(max_length=64), NoNul] = None,
    blocking: Annotated[Literal["0", "1"], Form()] = "1",
    target: Annotated[db.Target, Form()] = "implementation",
    revision: Annotated[int | None, Form()] = None,
):
    report = _require_report(conn, id)
    body = _require_body(body)
    revision = _revision(target, revision, version)
    user_id, name = user
    if not name:
        return _name_prompt(request, _document_url(report, target))

    def validate(markdown, current_revision):
        sections = split_sections(markdown)
        same = [s for s in sections if s.heading == section]
        if section is not None and not same:
            return "unknown section"
        if quote and (section is None or not quote_found(normalize(quote), same)):
            return "quote not found"
        return None

    try:
        row = db.add_comment(
            conn,
            report_id=id,
            version=revision,
            target=target,
            expected_version=version,
            author_id=user_id,
            author_name=name,
            body=body,
            section=section,
            quote=quote or None,
            blocking=blocking == "1",
            prefix=prefix or None,
            suffix=suffix or None,
            validate=validate,
        )
    except (db.StaleVersion, db.StaleRevision, db.MissingDocument, ValueError) as error:
        return _action_error(request, conn, report, target, error)
    settings = request.app.state.settings
    background.add_task(notify.send, settings, notify.comment_text(report, row, settings.base_url))
    return _thread_response(
        request, conn, report, user, row["id"], reset_form=True, version=version
    )


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
    target: Annotated[db.Target | None, Form()] = None,
    revision: Annotated[int | None, Form()] = None,
):
    report = _require_report(conn, id)
    body = _require_body(body)
    root = db.thread_root(conn, id, cid)
    if root is None:
        raise HTTPException(404, "comment not found")
    selected = root["target"]
    revision = _revision(selected, revision, version)
    user_id, name = user
    if not name:
        return _name_prompt(request, _document_url(report, selected))
    try:
        row = db.add_comment(
            conn,
            report_id=id,
            version=revision,
            target=target,
            expected_version=version,
            author_id=user_id,
            author_name=name,
            body=body,
            parent_id=cid,
            reopen=_can_moderate(_shared(request), request.state.member, user_id, root, report),
        )
    except (db.StaleVersion, db.StaleRevision, db.MissingDocument, ValueError) as error:
        return _action_error(request, conn, report, selected, error)
    settings = request.app.state.settings
    background.add_task(notify.send, settings, notify.comment_text(report, row, settings.base_url))
    return _thread_response(request, conn, report, user, row["parent_id"], version=version)


def _thread_prompt(request, conn, report, user, cid):
    root = db.thread_root(conn, report["id"], cid)
    if root is None:
        raise HTTPException(404, "comment not found")
    if not user[1]:
        return _name_prompt(request, _document_url(report, root["target"]), f"#vf_t{root['id']}")
    return None


@router.post("/r/{id}/comments/{cid}/resolve")
def resolve(
    id: str,
    cid: int,
    request: Request,
    conn: Conn,
    user: User,
    version: Annotated[int | None, Form()] = None,
):
    report = _require_report(conn, id)
    refusal = _thread_prompt(request, conn, report, user, cid) or _moderation_refusal(
        request, conn, report, user, cid, version=version
    )
    if refusal:
        return refusal
    root_id = db.resolve_thread(conn, id, cid, by=user[0] if _shared(request) else None)
    if root_id is None:
        raise HTTPException(404, "comment not found")
    return _thread_response(request, conn, report, user, root_id, version=version)


@router.post("/r/{id}/comments/{cid}/reopen")
def reopen(
    id: str,
    cid: int,
    request: Request,
    conn: Conn,
    user: User,
    version: Annotated[int | None, Form()] = None,
):
    report = _require_report(conn, id)
    prompt = _thread_prompt(request, conn, report, user, cid)
    if prompt:
        return prompt
    if not db.reopen_thread(conn, id, cid):
        raise HTTPException(404, "comment not found")
    return _thread_response(request, conn, report, user, cid, version=version)


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
    refusal = _thread_prompt(request, conn, report, user, cid) or _moderation_refusal(
        request, conn, report, user, cid, version=version
    )
    if refusal:
        return refusal
    root = db.thread_root(conn, id, cid)
    try:
        changed = db.set_blocking(conn, id, cid, blocking == "1", expected_version=version)
    except db.StaleVersion as error:
        return _action_error(request, conn, report, root["target"], error)
    if not changed:
        raise HTTPException(404, "comment not found")
    return _thread_response(request, conn, report, user, cid, version=version)


@router.post("/r/{id}/verdict")
def post_verdict(
    id: str,
    request: Request,
    conn: Conn,
    user: User,
    background: BackgroundTasks,
    version: Annotated[int, Form()],
    verdict: Annotated[Literal["approve", "request_changes"], Form()],
    summary: Annotated[str, Form(max_length=2000), NoNul] = "",
    target: Annotated[db.Target, Form()] = "implementation",
    revision: Annotated[int | None, Form()] = None,
):
    report = _require_report(conn, id)
    revision = _revision(target, revision, version)
    user_id, name = user
    if not name:
        return _name_prompt(request, _document_url(report, target))

    def validate() -> str | None:
        signature = db.get_signature(conn, id, revision, target=target)
        if signature is None:
            return UNSIGNED
        if signature["signer_id"] == user_id:
            return SELF_REVIEW
        threads = build_threads(db.list_comments(conn, id, target=target), [], revision)
        if verdict == "approve" and not rules.can_approve(threads):
            if not _shared(request):
                return BLOCKED_LOCAL
            mine = all(t.root["author_id"] == user_id for t in rules.open_blocking(threads))
            return BLOCKED_OWN if mine else BLOCKED
        if verdict == "request_changes" and not rules.can_request_changes(
            threads, user_id, summary
        ):
            return REFUSED
        return None

    try:
        row = db.add_verdict(
            conn,
            report_id=id,
            version=revision,
            target=target,
            expected_version=version,
            reviewer_id=user_id,
            reviewer_name=name,
            verdict=verdict,
            summary=summary.strip(),
            validate=validate,
        )
    except (
        db.StaleVersion,
        db.StaleRevision,
        db.PlanNotApproved,
        db.MissingDocument,
        ValueError,
    ) as error:
        return _action_error(request, conn, report, target, error)
    except db.VerdictRefused as refused:
        return _action_response(
            request, conn, id, user, target, version=version, error=str(refused), summary=summary
        )
    settings = request.app.state.settings
    background.add_task(notify.send, settings, notify.verdict_text(report, row, settings.base_url))
    return _action_response(request, conn, id, user, target, version=version)


@router.post("/r/{id}/sign")
def sign(
    id: str,
    request: Request,
    conn: Conn,
    user: User,
    background: BackgroundTasks,
    version: Annotated[int, Form()],
    target: Annotated[db.Target, Form()] = "implementation",
    revision: Annotated[int | None, Form()] = None,
):
    report = _require_report(conn, id)
    revision = _revision(target, revision, version)
    user_id, name = user
    if not name:
        return _name_prompt(request, _document_url(report, target))
    refusal = _sign_refusal(request, report, user_id)
    if refusal:
        raise HTTPException(403, refusal)
    try:
        signature = db.sign_version(
            conn,
            report_id=id,
            version=revision,
            target=target,
            expected_version=version,
            signer_id=user_id,
            signer_name=name,
        )
    except (
        db.StaleVersion,
        db.StaleRevision,
        db.PlanNotApproved,
        db.MissingDocument,
        ValueError,
    ) as error:
        return _action_error(request, conn, report, target, error)
    except db.AlreadySigned:
        pass
    else:
        with _read(conn):
            report = _require_report(conn, id)
            document = db.target_document(conn, id, version, target)
            settings = request.app.state.settings
            text = notify.ready_for_review_text(
                report, revision, name, settings.base_url, target=target
            )
            background.add_task(notify.send, settings, text)
            others = [p for p in db.requested_reviewers(conn, id) if p != user_id]
            _email_reviewers(
                request,
                background,
                conn,
                report,
                revision,
                others,
                target=target,
                markdown=document[1],
                signature=signature,
            )
    return _action_response(request, conn, id, user, target, version=version, signing=True)


def _claim_mail(request: Request, keys: list[tuple[str, str, str, int]]) -> list[str]:
    mailed = request.app.state.mailed
    with _MAIL_LOCK:
        new = [key for key in dict.fromkeys(keys) if key not in mailed]
        mailed.update(new)
    return [reviewer for _, reviewer, _, _ in new]


def _email_reviewers(
    request,
    background,
    conn,
    report,
    revision,
    recipients,
    added_by=None,
    *,
    target,
    markdown,
    signature,
):
    settings = request.app.state.settings
    if not (
        settings.smtp_url and _shared(request) and signature is not None and markdown is not None
    ):
        return
    users = {u["id"]: u for u in db.list_users(conn)}
    recipients = [
        p for p in recipients if p in users and users[p]["active"] and p != signature["signer_id"]
    ]
    recipients = _claim_mail(request, [(report["id"], p, target, revision) for p in recipients])
    if not recipients:
        return
    judge = section_plain_text(markdown, "Judge this")
    requesters = db.review_requesters(conn, report["id"])
    letters = {}
    for person in recipients:
        by = requesters.get(person)
        signed_by_asker = by == signature["signer_id"]
        named = by in users and by != person and not signed_by_asker
        subject, letters[person] = notify.review_email(
            report,
            revision,
            judge,
            settings.base_url,
            signer=None if added_by and signed_by_asker else signature["signer_name"],
            asker=added_by or (users[by]["name"] if named else None),
            added=added_by is not None,
            target=target,
        )
    background.add_task(
        _send_mail, request.app, report["id"], revision, subject, letters, target=target
    )


async def _send_mail(
    app,
    report_id: str,
    version: int,
    subject: str,
    letters: dict[str, str],
    *,
    target="implementation",
):
    delivered = await notify.email(app.state.settings, report_id, subject, letters)
    with _MAIL_LOCK:
        app.state.mailed.difference_update(
            (report_id, person, target, version) for person in letters if person not in delivered
        )


def _reviewers_response(
    request: Request,
    conn: db.Connection,
    report,
    user,
    error=None,
    row=None,
    status=None,
    *,
    target="implementation",
):
    if not request.headers.get("HX-Request"):
        if error:
            raise HTTPException(422, error)
        return RedirectResponse(_document_url(report, target), 303)
    with _read(conn):
        ctx = _response_context(request, conn, report["id"], user, target)
        ctx |= {"reviewer_error": error, "error_row": row, "reviewer_status": status}
        return _partial(request, ["_sidebar.html"], ctx, tail=_nav_oob(request, conn, user, ctx))


@router.post("/r/{id}/reviewers")
def request_review(
    id: str,
    request: Request,
    conn: Conn,
    user: User,
    member: Member,
    background: BackgroundTasks,
    reviewer_id: Annotated[str, Form(max_length=256), NoNul] = "",
    target: Annotated[db.Target, Form()] = "implementation",
):
    report = _require_report(conn, id)
    person = db.get_user(conn, reviewer_id.strip().lower())
    if person is None or not person["active"] or person["id"] == report["author_id"]:
        return _reviewers_response(request, conn, report, user, REVIEWER_REFUSED, target=target)
    if db.add_review_request(conn, id, person["id"], requested_by=user[0]):
        log.info(
            "access action=add_reviewer actor=%s target=%s report=%s", user[0], person["id"], id
        )
        with _read(conn):
            report = _require_report(conn, id)
            latest = db.latest_version(conn, id)
            active = _selected_target(conn, report, latest, None)
            ctx = report_context(
                conn, report, latest, user[0], request.state.member, _shared(request), target=active
            )
            if person["id"] != user[0] and ctx["stage"] != "awaiting_implementation":
                document = db.target_document(conn, id, latest, active)
                _email_reviewers(
                    request,
                    background,
                    conn,
                    report,
                    ctx["revision"],
                    [person["id"]],
                    user[1],
                    target=active,
                    markdown=document[1] if document else None,
                    signature=ctx["signature"],
                )
    status = f"Requested {_labels(db.list_users(conn))[person['id']]}."
    return _reviewers_response(request, conn, report, user, status=status, target=target)


@router.post("/r/{id}/reviewers/{reviewer_id:path}/remove")
def remove_review(
    id: str,
    reviewer_id: str,
    request: Request,
    conn: Conn,
    user: User,
    target: Annotated[db.Target, Form()] = "implementation",
):
    report = _require_report(conn, id)
    if _shared(request):
        reviewer_id = reviewer_id.lower()
    outcome = db.remove_review_request(conn, id, reviewer_id)
    label = _labels(db.list_users(conn)).get(reviewer_id, reviewer_id)
    if outcome == "kept":
        error = REVIEWER_KEPT.format(name=label)
        return _reviewers_response(
            request, conn, report, user, error, row=reviewer_id, target=target
        )
    if outcome == "removed":
        log.info(
            "access action=remove_reviewer actor=%s target=%s report=%s", user[0], reviewer_id, id
        )
        status = f"Removed the request for {label}."
    else:
        status = f"{label} had no review request."
    return _reviewers_response(request, conn, report, user, status=status, target=target)


@router.post("/me")
def set_name(request: Request, name: Annotated[str, Form()], next: Annotated[str, Form()] = "/"):
    if _shared(request):
        raise HTTPException(403, "identity is set by the proxy")
    name = name.strip()
    if not next.startswith("/") or next.startswith(("//", "/\\")):
        next = "/"
    if not _fits(name, 60):
        return RedirectResponse(f"{next}{'&' if '?' in next else '?'}name_error=1", 303)
    response = RedirectResponse(next, status_code=303)
    response.set_cookie(
        "vf_name", url_quote(name), max_age=YEAR, samesite="lax", secure=_secure(request)
    )
    return response


def _access(
    request: Request,
    conn: db.Connection,
    user,
    page: Literal["tokens", "members"],
    *,
    error=None,
    token=None,
    token_note=None,
    member_error=None,
    member_form=None,
    status=200,
):
    member = request.state.member
    ctx = {"active": page, "error": error}
    if member is not None and page == "tokens":
        ctx |= {
            "new_token": token,
            "token_note": token_note,
            "nonce": secrets.token_urlsafe(16),
            "tokens": db.list_tokens(conn, member["id"]),
        }
        if member["role"] == "admin":
            ctx["all_tokens"] = db.list_tokens(conn)
            ctx["names"] = {u["id"]: u["name"] for u in db.list_users(conn)}
    elif member is not None:
        ctx |= {
            "me": member["id"],
            "users": db.list_users(conn),
            "roles": db.ROLES,
            "member_error": member_error,
            "member_form": member_form or {"id": "", "name": "", "role": "member"},
        }
    return _page(request, conn, user, f"{page}.html", ctx, status)


def _denied(request: Request, message: str, back: str | None = "/access/tokens", home: bool = True):
    ctx = {"message": message, "back": back, "home": home}
    return templates.TemplateResponse(request, "denied.html", ctx, status_code=403)


@router.get("/access")
def access_page():
    return RedirectResponse("/access/tokens", 303)


@router.get("/access/tokens")
def tokens_page(request: Request, conn: Conn, user: User):
    return _access(request, conn, user, "tokens")


@router.get("/access/members")
def members_page(request: Request, conn: Conn, user: User):
    member = request.state.member
    if member is not None and member["role"] != "admin":
        return _denied(request, ADMIN_ONLY)
    return _access(request, conn, user, "members")


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
        return _access(
            request, conn, user, "members", member_error=error, member_form=form, status=status
        )

    clean_id, clean_name = id.strip().lower(), name.strip()
    if not (_fits(clean_id, 200) and _fits(clean_name, 60)):
        return refuse(MEMBER_INPUT, 422)
    if clean_id == "agent":
        return refuse(MEMBER_AGENT, 422)
    if not plain_id(clean_id):
        return refuse(MEMBER_PLAIN, 422)
    try:
        db.add_user(conn, id=clean_id, name=clean_name, role=role)
    except ValueError:
        return refuse(MEMBER_EXISTS, 409)
    log.info("access action=add_user actor=%s target=%s role=%s", user[0], clean_id, role)
    return RedirectResponse("/access/members", 303)


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
    try:
        row, revoked = db.update_user(
            conn, id, role=role, active=None if active is None else active == "1"
        )
    except db.LastAdmin:
        return _access(request, conn, user, "members", error=LAST_ADMIN, status=409)
    except db.InactiveMember as e:
        return _access(request, conn, user, "members", error=str(e), status=409)
    except ValueError:
        raise HTTPException(404, "member not found") from None
    log.info(
        "access action=update_user actor=%s target=%s role=%s active=%s tokens_revoked=%d",
        user[0],
        id,
        row["role"],
        row["active"],
        revoked,
    )
    return RedirectResponse("/access/members", 303)


@router.post("/access/tokens")
def new_token(
    request: Request,
    conn: Conn,
    user: User,
    member: Member,
    label: Annotated[str, Form()] = "",
    nonce: Annotated[str, Form(max_length=64), NoNul] = "",
):
    label = label.strip()
    if not _fits(label, 60):
        return _access(request, conn, user, "tokens", error=LABEL_INPUT, status=422)
    try:
        token, row = db.create_token(conn, owner_id=member["id"], label=label, nonce=nonce or None)
    except db.AlreadyCreated:
        return _access(request, conn, user, "tokens", token_note=ALREADY_CREATED)
    except ValueError:
        return _denied(request, DEACTIVATED.format(id=member["id"]), back=None, home=False)
    log.info("access action=create_token actor=%s target=%s", user[0], row["id"])
    return _access(request, conn, user, "tokens", token=token)


@router.post("/access/tokens/{token_id}/revoke")
def revoke_token(token_id: str, request: Request, conn: Conn, user: User, member: Member):
    token = db.get_token(conn, token_id)
    if token is None:
        raise HTTPException(404, "token not found")
    if token["owner_id"] != member["id"] and member["role"] != "admin":
        return _denied(request, OWNER_ONLY)
    if db.revoke_token(conn, token_id):
        log.info("access action=revoke_token actor=%s target=%s", user[0], token_id)
    return RedirectResponse("/access/tokens", 303)
