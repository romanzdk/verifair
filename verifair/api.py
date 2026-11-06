import asyncio
import json
import logging
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime
from itertools import islice
from pathlib import Path
from time import monotonic
from typing import Annotated, Literal, NoReturn
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from verifair import db, notify
from verifair.deps import get_conn, run_with_conn
from verifair.render import (
    build_threads,
    iter_occurrences,
    normalize,
    quote_found,
    section_headings,
    split_sections,
)
from verifair.rules import NO_OWNER, compute_status, latest_verdicts, review_stage
from verifair.traces import TraceDocument

NOT_OWNER = "This report belongs to another member."
ARCHIVED = "This report is archived. Restore it before pushing a new version."
NOT_ARCHIVER = (
    "Only the report's owner and its requested reviewers can archive or restore it with a token."
)
SHARED_TOKEN = "The shared token is not accepted in shared mode. Use a member agent token."
AGENT_RESOLVE = "Agent tokens can only resolve threads the agent started on its owner's reports."
log = logging.getLogger("verifair")


def _refuse_token(reason: str, path: str, detail: str = "invalid token") -> NoReturn:
    log.info("api token refused reason=%s path=%r", reason, path)
    raise HTTPException(401, detail)


async def require_token(request: Request) -> None:
    settings = request.app.state.settings
    given = request.headers.get("Authorization", "")
    request.state.token_owner = request.state.token = None
    if settings.shared:
        path = request.url.path
        scheme, _, token = given.partition(" ")
        if not given:
            _refuse_token("missing", path)
        if scheme != "Bearer" or not token:
            _refuse_token("malformed", path)
        if not token.lstrip().startswith("vf_"):
            _refuse_token("shared-token", path, SHARED_TOKEN)
        owner, reason = await run_with_conn(request.app, db.authenticate_token, token, touch=True)
        if owner is None:
            _refuse_token(reason, path)
        request.state.token, request.state.token_owner = token, owner
        return
    token = settings.api_token
    if not token or not secrets.compare_digest(given.encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, "invalid token")


router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])
SKILL = Path(__file__).parents[1] / "skills" / "verifair-review" / "SKILL.md"


@router.get("/skill")
def skill():
    # served by the server itself, so an agent always gets the skill that matches this version
    if not SKILL.is_file():
        raise HTTPException(404, "this server was installed without the skill")
    return PlainTextResponse(SKILL.read_text(encoding="utf-8"), media_type="text/markdown")


# "function" returns the connection when the handler ends, not after the response and its
# background tasks (Slack notify) are done
Conn = Annotated[db.Connection, Depends(get_conn, scope="function")]


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


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*")
    @classmethod
    def _no_nul(cls, value):
        for text in value if isinstance(value, list) else [value]:
            if isinstance(text, str):
                db.no_nul(text)
        return value


class Link(Input):
    label: str
    url: HttpUrl


class ReportIn(Input):
    project: str = Field(pattern=SLUG)
    title: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=200), AfterValidator(_not_blank)
    ]
    risk: Literal["low", "medium", "high"]
    body: Annotated[str, AfterValidator(_not_blank)] | None = None
    plan: Annotated[str, AfterValidator(_not_blank)] | None = None
    workflow: db.Workflow | None = None
    expected_version: int | None = Field(default=None, gt=0)
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
    trace: TraceDocument | None = None


class CommentIn(Input):
    body: Annotated[
        str, StringConstraints(strip_whitespace=True, max_length=20000), AfterValidator(_not_blank)
    ]
    author: Author = "agent"
    parent_id: int | None = None
    section: Anchor | None = None
    quote: Anchor | None = None
    prefix: Context | None = None
    suffix: Context | None = None
    blocking: bool = True
    target: db.Target | None = None
    revision: int | None = Field(default=None, gt=0)


def _require_report(conn: db.Connection, id: str) -> db.Row:
    report = db.get_report(conn, id)
    if report is None:
        raise HTTPException(404, "report not found")
    return report


def _require_owner(report: db.Row, owner: db.Row | None) -> None:
    if owner is None:
        return
    if report["author_id"] is None:
        raise HTTPException(403, NO_OWNER)
    if report["author_id"] != owner["id"]:
        raise HTTPException(403, NOT_OWNER)


@contextmanager
def _read(conn: db.Connection):
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        yield


def _write_error(error: Exception) -> JSONResponse:
    if isinstance(error, db.StaleVersion):
        return JSONResponse({"detail": "review changed", "current_version": error.latest}, 409)
    if isinstance(error, db.StaleRevision):
        return JSONResponse(
            {
                "detail": str(error),
                "target": error.target,
                "current_revision": error.current_revision,
            },
            409,
        )
    if isinstance(error, db.PlanNotApproved):
        return JSONResponse({"detail": str(error), "plan_revision": error.plan_revision}, 409)
    return JSONResponse(
        {"detail": str(error)},
        409 if isinstance(error, (db.WorkflowMismatch, db.MissingDocument)) else 422,
    )


def _feedback_identity(conn: db.Connection, id: str, target: db.Target):
    report = _require_report(conn, id)
    if target == "plan" and report["workflow"] != "plan_first":
        raise HTTPException(422, "artifact plans are read-only")
    current = db.latest_version(conn, id)
    document = db.target_document(conn, id, current, target)
    return {
        "current_version": current,
        "target": target,
        "revision": document[0] if document else None,
    }, document


def _comment_json(row: db.Row, root: db.Row | None = None) -> dict:
    root = row if root is None else root
    return {
        "id": row["id"],
        "version": row["version"],
        "target": row["target"],
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


def _latest_ids(verdicts: list[db.Row], current: int) -> set[int]:
    return {row["id"] for row in latest_verdicts(verdicts, current).values()}


def _verdict_json(row: db.Row, latest_ids: set[int]) -> dict:
    return {
        "reviewer": row["reviewer_name"],
        "reviewer_id": row["reviewer_id"],
        "verdict": row["verdict"],
        "version": row["version"],
        "target": row["target"],
        "summary": row["summary"],
        "created_at": row["created_at"],
        "current": row["id"] in latest_ids,
    }


PROVENANCE = {"agent", "model", "session_url"}


def _metadata_supplied(sent: set[str]) -> set[str]:
    # agent, model and session_url name one writer, so a push replaces them together
    return sent | PROVENANCE if sent & PROVENANCE else sent


@router.post("/reports", status_code=201)
def push_report(body: ReportIn, request: Request, conn: Conn, background: BackgroundTasks):
    if body.body is not None:
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
            body_supplied="body" in body.model_fields_set,
            workflow=body.workflow,
            expected_version=body.expected_version,
            plan=body.plan,
            inherit_plan="plan" not in body.model_fields_set,
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
            trace=body.trace,
            trace_supplied="trace" in body.model_fields_set,
            metadata_supplied=_metadata_supplied(body.model_fields_set),
            owner_id=owner["id"] if owner is not None else None,
        )
    except db.NoOwner:
        raise HTTPException(403, NO_OWNER) from None
    except db.NotOwner:
        raise HTTPException(403, NOT_OWNER) from None
    except db.ProjectMismatch:
        raise HTTPException(409, "id belongs to another project") from None
    except db.Archived:
        raise HTTPException(409, ARCHIVED) from None
    except (db.StaleVersion, db.PlanNotApproved, db.WorkflowMismatch, ValueError) as error:
        return _write_error(error)
    settings = request.app.state.settings
    base_url = settings.base_url
    with _read(conn):
        report = _report_json(conn, id, snapshot_version=version)
    target = "implementation" if report["stage"] == "implementation" else "plan"
    revision = (
        report["current_version"]
        if target == "implementation"
        else report["plan_review"]["revision"]
    )
    background.add_task(
        notify.send, settings, notify.report_pushed_text(report, revision, base_url, target=target)
    )
    return {"id": id, "version": version, "url": f"{base_url}/r/{id}"}


def _set_archived(id: str, request: Request, conn: db.Connection, archive: bool):
    report = _require_report(conn, id)
    owner = request.state.token_owner
    if owner is not None and not (
        report["author_id"] == owner["id"] or owner["id"] in db.requested_reviewers(conn, id)
    ):
        raise HTTPException(403, NOT_ARCHIVER)
    by = owner["name"] if owner is not None else "agent"
    if db.set_archived(conn, id, by if archive else None):
        actor = owner["id"] if owner is not None else "agent"
        action = "archive" if archive else "restore"
        log.info("access action=%s actor=%s report=%s", action, actor, id)
    with _read(conn):
        return _report_json(conn, id)


@router.post("/reports/{id}/archive")
def archive_report(id: str, request: Request, conn: Conn):
    return _set_archived(id, request, conn, archive=True)


@router.post("/reports/{id}/restore")
def restore_report(id: str, request: Request, conn: Conn):
    return _set_archived(id, request, conn, archive=False)


@router.get("/reports/{id}")
def get_report(id: str, conn: Conn):
    with _read(conn):
        return _report_json(conn, id)


def _report_json(conn: db.Connection, id: str, *, snapshot_version: int | None = None):
    report = _require_report(conn, id)
    current = db.latest_version(conn, id) if snapshot_version is None else snapshot_version
    snapshot = db.get_version(conn, id, current)
    verdicts = db.list_verdicts(conn, id)
    signature = db.get_signature(conn, id, current) if snapshot["body"] is not None else None
    latest_ids = _latest_ids(verdicts, current)
    implementation_status = (
        compute_status(
            verdicts,
            current,
            signed=signature is not None,
            signer_id=signature["signer_id"] if signature else None,
        )
        if snapshot["body"] is not None
        else None
    )
    plan = db.plan_review_state(conn, id, current)
    stage, status = review_stage(
        workflow=report["workflow"],
        has_body=snapshot["body"] is not None,
        plan_status=plan["status"] if plan else None,
        implementation_status=implementation_status,
    )
    plan_review = None
    if plan is not None:
        signed = plan["signature"]
        plan_latest_ids = _latest_ids(plan["verdicts"], plan["revision"])
        plan_review = {
            "revision": plan["revision"],
            "status": plan["status"],
            "signed_by": signed["signer_name"] if signed else None,
            "signed_at": signed["created_at"] if signed else None,
            "open_blocking": plan["open_blocking"],
            "verdicts": [_verdict_json(v, plan_latest_ids) for v in plan["verdicts"]],
        }
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
        "status": status,
        "workflow": report["workflow"],
        "archived_at": report["archived_at"],
        "archived_by": report["archived_by"],
        "stage": stage,
        "implementation_status": implementation_status,
        "plan_review": plan_review,
        "current_version": current,
        "versions": [
            {
                "version": v["version"],
                "created_at": v["created_at"],
                "commit": v["commit"],
                "has_implementation": v["body"] is not None,
                "has_trace": v["has_trace"],
                "plan_revision": v["plan_revision"],
            }
            for v in db.list_versions(conn, id)
        ],
        "verdicts": [_verdict_json(v, latest_ids) for v in verdicts],
    }


@router.get("/reports/{id}/versions/{n}")
def get_version(id: str, n: int, conn: Conn):
    with _read(conn):
        _require_report(conn, id)
        snapshot = db.get_version(conn, id, n)
        if snapshot is None:
            raise HTTPException(404, "version not found")
        return {
            key: snapshot[key]
            for key in ("version", "created_at", "commit", "body", "plan", "plan_revision")
        } | {"trace": json.loads(snapshot["trace"]) if snapshot["trace"] is not None else None}


def _poll_wait(
    conn: db.Connection,
    url_path: str,
    id: str,
    kind: str,
    since: datetime | None,
    after: dict[str, int] | None,
    token: str | None,
    target: db.Target,
) -> tuple[dict, dict[str, int]]:
    if token is not None and (reason := db.authenticate_token(conn, token)[1]):
        _refuse_token(reason, url_path)
    with _read(conn):
        identity, document = _feedback_identity(conn, id, target)
        revision = identity["revision"]
        rows = {}
        if kind in ("any", "verdict"):
            rows["verdict"] = db.list_verdicts(conn, id, target=target) if document else []
        if kind in ("any", "comment"):
            rows["comment"] = db.list_comments(conn, id, target=target) if document else []
        if since is None and after is None:
            return identity, {
                event: max((r["id"] for r in items), default=0) for event, items in rows.items()
            }
        latest_ids = _latest_ids(rows.get("verdict", []), revision)
        events = []
        for event, items in rows.items():
            for row in items:
                if row["version"] != revision or (
                    event == "comment" and row["parent_id"] is not None
                ):
                    continue
                created = datetime.fromisoformat(row["created_at"])
                if since is not None:
                    if created <= since:
                        continue
                elif row["id"] <= after[event]:
                    continue
                value = _verdict_json(row, latest_ids) if event == "verdict" else _comment_json(row)
                events.append(
                    (created, {"event": event, event: value, "next_since": row["created_at"]})
                )
        event = min(events, key=lambda e: e[0])[1] if events else {}
        return identity | event, after or {}


@router.get("/reports/{id}/wait")
async def wait_for_event(
    id: str,
    request: Request,
    kind: Annotated[Literal["verdict", "comment", "any"], Query(alias="for")] = "any",
    since: datetime | None = None,
    timeout: Annotated[float, Query(gt=0, le=55)] = 55,
    target: db.Target = "implementation",
):
    if since is not None:
        since = since.replace(tzinfo=UTC) if since.tzinfo is None else since.astimezone(UTC)
    cursor = (since or datetime.now(UTC)).isoformat()
    deadline = monotonic() + timeout
    after = None
    while True:
        event, after = await run_with_conn(
            request.app,
            _poll_wait,
            request.url.path,
            id,
            kind,
            since,
            after,
            request.state.token,
            target,
        )
        if "event" in event:
            return {"since": cursor, **event}
        remaining = deadline - monotonic()
        if remaining <= 0:
            return {"event": "timeout", "since": cursor, "next_since": cursor, **event}
        await asyncio.sleep(min(0.5, remaining))


@router.get("/reports/{id}/comments")
def get_comments(id: str, conn: Conn, open: bool = False, target: db.Target = "implementation"):
    with _read(conn):
        identity, document = _feedback_identity(conn, id, target)
        if document is None:
            return {"threads": [], **identity}
        revision, markdown = document
        comments = db.list_comments(conn, id, open_only=open, target=target)
        threads = build_threads(comments, split_sections(markdown), revision)
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
                        _comment_json(r, t.root) | anchor | {"blocking": t.blocking}
                        for r in t.replies
                    ],
                }
            )
        return {"threads": result, **identity}


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
    headings = []

    def validate(markdown: str, revision: int) -> str | None:
        nonlocal headings
        if body.parent_id is not None or body.section is None:
            return None
        sections = split_sections(markdown)
        headings = list(dict.fromkeys(s.heading for s in sections))
        if body.section not in headings:
            return "unknown section"
        quote = normalize(body.quote or "")
        same = [s for s in sections if s.heading == body.section]
        if quote and not quote_found(quote, same):
            raise HTTPException(422, "quote not found")
        if quote and not (body.prefix or body.suffix):
            hits = sum(
                1
                for _ in islice(
                    (start for s in same for start in iter_occurrences(s.text, quote)), 2
                )
            )
            if hits > 1:
                raise HTTPException(422, "ambiguous quote")
        return None

    reopen = True
    if owner is not None and body.parent_id is not None:
        root = db.thread_root(conn, id, body.parent_id)
        reopen = root is not None and root["author_id"] == "agent"
    try:
        row = db.add_comment(
            conn,
            report_id=id,
            version=body.revision,
            target=body.target,
            validate=validate,
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
    except ValueError as error:
        if str(error).startswith("parent comment "):
            raise HTTPException(404, "parent not found") from None
        if str(error) == "unknown section":
            return JSONResponse({"detail": "unknown section", "sections": headings}, 422)
        return _write_error(error)
    except (db.StaleVersion, db.StaleRevision, db.PlanNotApproved, db.MissingDocument) as error:
        return _write_error(error)
    settings = request.app.state.settings
    background.add_task(notify.send, settings, notify.comment_text(report, row, settings.base_url))
    with _read(conn):
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
