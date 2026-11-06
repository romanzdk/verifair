import asyncio
import ipaddress
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import psycopg
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException

from verifair import api, auth, db, web
from verifair.config import Settings, check_oidc
from verifair.deps import borrow, run_with_conn

TOO_LARGE = {"detail": "request body too large"}
CSP = (
    "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline';"
    " base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
# the API explorer pages load pinned Swagger UI and ReDoc from jsDelivr, checked by SRI hashes,
# and start them with an inline script
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;"
    " style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https:;"
    " worker-src blob:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
CDN = "https://cdn.jsdelivr.net/npm"
SWAGGER = f"{CDN}/swagger-ui-dist@5.34.1"
SWAGGER_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>verifair API</title>
<link rel="stylesheet" href="{SWAGGER}/swagger-ui.css" crossorigin="anonymous"
 integrity="sha384-4c3vsE5f+trhFyHrFrLM1fFGUn531NIaC7dcqEWIJdoRQJ5CkJbSuSCwXLpaS1KF">
</head><body><div id="swagger-ui"></div>
<script src="{SWAGGER}/swagger-ui-bundle.js" crossorigin="anonymous"
 integrity="sha384-XFYN47VzPiDY6qPtxYKg0nqUeW/upo20xf1j2SUE7gT/i35FJB4cuS/71Iegzk8Z"></script>
<script>SwaggerUIBundle({{url: "/openapi.json", dom_id: "#swagger-ui"}})</script>
</body></html>"""
REDOC_PAGE = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>verifair API</title></head><body>
<redoc spec-url="/openapi.json"></redoc>
<script src="{CDN}/redoc@2.5.4/bundles/redoc.standalone.js" crossorigin="anonymous"
 integrity="sha384-w447zOpYfw/1Tv/5AK9NfHTlQIqE3RVR6KY62jCyy9zNDgO64cMwGGP1Fj0zJVf5"></script>
</body></html>"""
MAX_BODY = 1024 * 1024
READYZ_TIMEOUT = 2
log = logging.getLogger("verifair")
SIGN_IN = "Sign in to use verifair."
PUBLIC = ("/healthz", "/readyz", "/login", "/auth/callback", "/logout", "/signed-out")
DB_UNAVAILABLE = "The database is not available. Try again in a moment."
# connection exceptions, and the server shutting down or not accepting connections yet
CONNECTION_LOST = ("08", "57P")
STARTUP_LOCKED = (
    "Startup waited too long for a database lock held by another transaction. Restart to try again."
)


UNKNOWN_HOST = (
    "Unknown host. In local mode verifair answers only on localhost, IP addresses and the host"
    " in VERIFAIR_BASE_URL."
)


def _local_host_allowed(host: str, settings: Settings) -> bool:
    # DNS rebinding points an attacker's domain at this machine, so in local mode, which has no
    # sign-in, a dotted name is only trusted when it is the configured address
    if "." not in host or host.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host == (urlsplit(settings.base_url).hostname or "")


def _loopback_own_origin(request: Request) -> bool:
    # browsers omit Sec-Fetch-Site on http://0.0.0.0, which is not a trustworthy origin. Only
    # literal loopback or unspecified hosts qualify, so plain-HTTP hostnames and DNS rebinding
    # stay refused.
    host = request.url.hostname or ""
    if host != "localhost":
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        if not (ip.is_loopback or ip.is_unspecified):
            return False
    return request.headers.get("Origin") == f"{request.url.scheme}://{request.url.netloc}"


def _sign_in_again(request: Request, reason: str, cookie: str | None):
    log.info("membership denied reason=%s path=%r", reason, request.url.path)
    # raw_path keeps an encoded slash in a project name encoded
    raw = request.scope.get("raw_path", b"").decode("latin-1") or request.url.path
    target = raw + (f"?{request.url.query}" if request.url.query else "")
    if request.method in ("GET", "HEAD") and "HX-Request" not in request.headers:
        response = RedirectResponse(f"/login?next={quote(target, safe='')}", 303)
    else:
        # the page the person is on, not the htmx or form URL that answered 401
        page = request.headers.get("HX-Current-URL") or request.headers.get("Referer")
        try:
            parts = urlsplit(page) if page else None
        except ValueError:
            parts = urlsplit("/")
        if parts:
            target = parts.path + (f"?{parts.query}" if parts.query else "")
        elif request.method not in ("GET", "HEAD"):
            target = "/"
        next = auth.safe_next(target)
        response = auth.page(request, "No access", SIGN_IN, 401, signin=True, next=next)
    if cookie:
        # a refused cookie that stays would send a failed sign-in back here in a loop
        auth.clear_cookie(request, response, auth.SESSION_COOKIE)
    return response


async def _membership_denial(request: Request, settings: Settings):
    cookie = request.cookies.get(auth.cookie_name(settings, auth.SESSION_COOKIE))
    session = auth.read_session(settings.session_secret, cookie) if cookie else None
    if session is None:
        return _sign_in_again(request, "bad-session" if cookie else "no-session", cookie)
    user_id, issued = session
    user = await run_with_conn(request.app, db.get_user, user_id)
    valid_after = user and user["sessions_valid_after"]
    if valid_after and issued < datetime.fromisoformat(valid_after).timestamp():
        return _sign_in_again(request, "signed-out", cookie)
    if user is not None and user["active"]:
        request.state.member = user
        return None
    log.info(
        "membership denied reason=%s path=%r",
        "not-member" if user is None else "inactive",
        request.url.path,
    )
    message = (web.NOT_MEMBER if user is None else web.DEACTIVATED).format(id=user_id)
    return auth.page(request, "No access", message, 403, signout=True)


class BodyLimit:
    def __init__(self, app, limit: int):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = Headers(scope=scope).get("content-length", "")
        if declared.isdigit() and int(declared) > self.limit:
            return await JSONResponse(TOO_LARGE, 413)(scope, receive, send)
        received = 0
        refused = False

        # Answering here, not raising from receive: BaseHTTPMiddleware turns an exception
        # raised in receive into an exception group, which FastAPI reports as a 400.
        async def counting_receive():
            nonlocal received, refused
            if refused:
                return {"type": "http.disconnect"}
            message = await receive()
            received += len(message.get("body", b""))
            if received > self.limit:
                refused = True
                await JSONResponse(TOO_LARGE, 413)(scope, receive, send)
                return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            if not refused:
                await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            if not refused:
                raise


def create_app(settings: Settings) -> FastAPI:
    check_oidc(settings)
    admin_id = settings.bootstrap_admin_id
    if admin_id and not settings.shared:
        raise RuntimeError(
            "VERIFAIR_BOOTSTRAP_ADMIN_ID is set but VERIFAIR_OIDC_ISSUER is not."
            " Set up sign-in to run in shared mode."
        )
    if admin_id and not web.email_id(admin_id):
        raise RuntimeError(
            "VERIFAIR_BOOTSTRAP_ADMIN_ID must be the email the admin signs in with,"
            " such as jane@example.com"
        )
    if settings.shared and settings.api_token:
        log.warning("VERIFAIR_API_TOKEN is ignored in shared mode. Agents need member tokens.")
    if settings.smtp_url and not settings.shared:
        log.warning("VERIFAIR_SMTP_URL is ignored in local mode. Emails need shared mode.")
    pool = db.open_pool(settings.database_url, settings.db_pool_size)
    try:
        # init_schema's advisory lock lasts until this commit, so replicas starting
        # together run the checks and the bootstrap one after another
        with pool.connection() as conn, conn.transaction():
            db.init_schema(conn)
            if not settings.shared and db.list_users(conn):
                raise RuntimeError(
                    "This database has members. Set VERIFAIR_OIDC_ISSUER to run in shared mode."
                )
            if settings.shared and admin_id:
                if db.bootstrap_admin(conn, admin_id):
                    log.info("bootstrap admin created id=%s", admin_id.lower())
                else:
                    log.info("bootstrap skipped: members exist")
            if settings.shared and not db.list_users(conn):
                raise RuntimeError(
                    "VERIFAIR_BOOTSTRAP_ADMIN_ID is required to create the first admin"
                )
            if settings.shared and (changed := db.lowercase_human_ids(conn)):
                log.info("lowercased ids rows=%d", changed)
    except psycopg.errors.LockNotAvailable as error:
        pool.close()
        raise RuntimeError(STARTUP_LOCKED) from error
    except BaseException:
        pool.close()
        raise

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        pool.close()

    app = FastAPI(
        title="verifair",
        version=web.VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/docs", include_in_schema=False)
    def api_explorer():
        return HTMLResponse(SWAGGER_PAGE)

    @app.get("/redoc", include_in_schema=False)
    def api_reference():
        return HTMLResponse(REDOC_PAGE)

    app.state.settings = settings
    app.state.pool = pool
    app.state.mailed = set()
    app.state.pool_slots = asyncio.Semaphore(settings.db_pool_size)
    app.state.oidc_transport = None
    app.state.oidc_config = None
    app.state.oidc_failure = None
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(api.router)
    app.include_router(web.router, include_in_schema=False)
    if settings.shared:
        app.include_router(auth.router, include_in_schema=False)

    @app.middleware("http")
    async def web_guard(request: Request, call_next):
        path = request.url.path
        is_api = path.startswith("/api/")
        request.state.member = None
        # Postgres text cannot hold NUL, so no stored id can match such a path
        response = JSONResponse({"detail": "Not Found"}, 404) if "\x00" in path else None
        if response is None and not settings.shared:
            if not _local_host_allowed((request.url.hostname or "").lower(), settings):
                response = PlainTextResponse(UNKNOWN_HOST, 400)
        try:
            if (
                response is None
                and settings.shared
                and not (is_api or path in PUBLIC or path.startswith("/static/"))
            ):
                response = await _membership_denial(request, settings)
            if response is None and request.method == "POST" and not is_api:
                site = request.headers.get("Sec-Fetch-Site")
                if site is None:
                    # shared mode runs on HTTPS or localhost, where browsers send Sec-Fetch-Site
                    if settings.shared or not _loopback_own_origin(request):
                        response = JSONResponse({"detail": "browser request required"}, 403)
                elif site not in ("same-origin", "none"):
                    response = PlainTextResponse("cross-origin request refused", 403)
            if response is None:
                response = await call_next(request)
        # caught here, not in an exception handler, because those do not see the membership
        # check above. PoolTimeout is an OperationalError without a SQLSTATE.
        except psycopg.OperationalError as error:
            if error.sqlstate is not None and not error.sqlstate.startswith(CONNECTION_LOST):
                raise
            log.error("database unavailable path=%r: %s: %s", path, type(error).__name__, error)
            response = PlainTextResponse(DB_UNAVAILABLE, 503, headers={"Retry-After": "5"})
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if not path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        if settings.https:
            # a day, so a broken certificate does not lock users out for long
            response.headers["Strict-Transport-Security"] = "max-age=86400"
        if response.headers.get("content-type", "").startswith("text/html"):
            explorer = path in ("/docs", "/redoc")
            response.headers["Content-Security-Policy"] = DOCS_CSP if explorer else CSP
        return response

    @app.exception_handler(StarletteHTTPException)
    async def not_found_page(request: Request, error: StarletteHTTPException):
        path = request.url.path
        browser_get = request.method == "GET" and not path.startswith(("/api/", "/static/"))
        if error.status_code == 404 and browser_get and not web._htmx(request):
            detail = error.detail if isinstance(error.detail, str) else ""
            known = detail and detail != "Not Found"
            message = f"{detail[:1].upper()}{detail[1:]}." if known else "This page does not exist."
            ctx = {"title": "Not found", "message": message, "home": True}
            return web.templates.TemplateResponse(request, "denied.html", ctx, status_code=404)
        return await http_exception_handler(request, error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        errors = error.errors()
        if request.url.path == "/api/reports":
            for detail in errors:
                loc = tuple(detail["loc"])
                if (
                    loc[:2] == ("body", "trace")
                    or loc == ("body",)
                    or detail["type"] == "json_invalid"
                    or isinstance(detail.get("input"), (dict, list))
                ):
                    detail.pop("input", None)
                    detail.pop("ctx", None)
                    if detail["type"] == "extra_forbidden":
                        detail["loc"] = loc[:-1]
        # the errors echo the input, and ensure_ascii escapes a lone surrogate there that
        # JSONResponse's UTF-8 encoding would turn into a 500. A NaN or Infinity in the
        # input has no strict JSON form, so then the echo is left out.
        errors = jsonable_encoder(errors)
        try:
            body = json.dumps({"detail": errors}, separators=(",", ":"), allow_nan=False)
        except ValueError:
            errors = [{k: v for k, v in e.items() if k != "input"} for e in errors]
            body = json.dumps({"detail": errors}, separators=(",", ":"))
        return Response(body, 422, media_type="application/json")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/readyz")
    async def readyz():
        try:
            async with borrow(app, READYZ_TIMEOUT) as conn:
                await run_in_threadpool(conn.execute, "SELECT 1")
        except psycopg.Error as error:
            log.error("not ready: %s: %s", type(error).__name__, error)
            return JSONResponse({"ok": False}, 503, headers={"Retry-After": "5"})
        return {"ok": True}

    app.add_middleware(BodyLimit, limit=MAX_BODY)
    return app
