import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import psycopg
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers

from verifair import api, auth, db, web
from verifair.config import Settings, check_oidc
from verifair.deps import borrow, run_with_conn

TOO_LARGE = {"detail": "request body too large"}
CSP = (
    "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline';"
    " frame-ancestors 'none'"
)
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


async def _membership_denial(request: Request, settings: Settings):
    cookie = request.cookies.get(auth.SESSION_COOKIE)
    user_id = auth.read_session(settings.session_secret, cookie) if cookie else None
    if user_id is None:
        log.info(
            "membership denied reason=%s path=%r",
            "bad-session" if cookie else "no-session",
            request.url.path,
        )
        if request.method in ("GET", "HEAD") and "HX-Request" not in request.headers:
            # raw_path keeps an encoded slash in a project name encoded
            raw = request.scope.get("raw_path", b"").decode("latin-1") or request.url.path
            target = raw + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(f"/login?next={quote(target, safe='')}", 303)
        return auth.page(request, "No access", SIGN_IN, 401, signin=True)
    user = await run_with_conn(request.app, db.get_user, user_id)
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
    if admin_id and not web.plain_id(admin_id):
        raise RuntimeError(
            "VERIFAIR_BOOTSTRAP_ADMIN_ID must be a plain id such as jane@example.com"
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

    app = FastAPI(title="verifair", lifespan=lifespan)
    app.state.settings = settings
    app.state.pool = pool
    app.state.mailed = set()
    app.state.pool_slots = asyncio.Semaphore(settings.db_pool_size)
    app.state.oidc_transport = None
    app.state.oidc_config = None
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(api.router)
    app.include_router(web.router)
    if settings.shared:
        app.include_router(auth.router)

    @app.middleware("http")
    async def web_guard(request: Request, call_next):
        path = request.url.path
        is_api = path.startswith("/api/")
        request.state.member = None
        # Postgres text cannot hold NUL, so no stored id can match such a path
        response = JSONResponse({"detail": "Not Found"}, 404) if "\x00" in path else None
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
        if path == "/access" or path.startswith("/access/"):
            response.headers["Cache-Control"] = "no-store"
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Content-Security-Policy"] = CSP
        return response

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
