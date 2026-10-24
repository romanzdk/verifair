import logging
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool

from verifair import api, db, web
from verifair.config import Settings

CSP = (
    "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline';"
    " frame-ancestors 'none'"
)
log = logging.getLogger("verifair")
SIGN_IN = "Sign in through your company login to use verifair."
STARTUP_LOCKED = (
    "Startup waited too long for a database lock held by another transaction. Restart to try again."
)


def _get_user(pool: ConnectionPool, user_id: str):
    with pool.connection() as conn:
        return db.get_user(conn, user_id)


async def _membership_denial(request: Request, settings: Settings):
    values = request.headers.getlist(settings.identity_header)
    user_id = values[0].strip().lower() if len(values) == 1 else ""
    message, status = SIGN_IN, 401
    if len(values) > 1:
        reason = "duplicate-header"
    elif user_id in ("", "agent"):
        reason = "missing-header"
    else:
        user = await run_in_threadpool(_get_user, request.app.state.pool, user_id)
        if user is not None and user["active"]:
            request.state.member = user
            return None
        reason = "not-member" if user is None else "inactive"
        status = 403
        message = (web.NOT_MEMBER if user is None else web.DEACTIVATED).format(id=user_id)
    log.info("membership denied reason=%s path=%r", reason, request.url.path)
    return web.templates.TemplateResponse(
        request, "denied.html", {"message": message, "home": status == 401}, status_code=status
    )


def create_app(settings: Settings) -> FastAPI:
    admin_id = settings.bootstrap_admin_id
    if admin_id and not settings.identity_header:
        raise RuntimeError(
            "VERIFAIR_BOOTSTRAP_ADMIN_ID is set but VERIFAIR_IDENTITY_HEADER is not."
            " Set the header to run in shared mode."
        )
    if admin_id and not web.plain_id(admin_id):
        raise RuntimeError(
            "VERIFAIR_BOOTSTRAP_ADMIN_ID must be a plain id such as jane@example.com"
        )
    if settings.identity_header and settings.api_token:
        log.warning("VERIFAIR_API_TOKEN is ignored in shared mode. Agents need member tokens.")
    pool = db.open_pool(settings.database_url, settings.db_pool_size)
    try:
        # init_schema's advisory lock lasts until this commit, so replicas starting
        # together run the checks and the bootstrap one after another
        with pool.connection() as conn, conn.transaction():
            db.init_schema(conn)
            if not settings.identity_header and db.list_users(conn):
                raise RuntimeError(
                    "This database has members. Set VERIFAIR_IDENTITY_HEADER to run in shared mode."
                )
            if settings.identity_header and admin_id:
                if db.bootstrap_admin(conn, admin_id):
                    log.info("bootstrap admin created id=%s", admin_id.lower())
                else:
                    log.info("bootstrap skipped: members exist")
            if settings.identity_header and not db.list_users(conn):
                raise RuntimeError(
                    "VERIFAIR_BOOTSTRAP_ADMIN_ID is required to create the first admin"
                )
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
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(api.router)
    app.include_router(web.router)

    @app.middleware("http")
    async def web_guard(request: Request, call_next):
        path = request.url.path
        is_api = path.startswith("/api/")
        request.state.member = None
        # Postgres text cannot hold NUL, so no stored id can match such a path
        response = JSONResponse({"detail": "Not Found"}, 404) if "\x00" in path else None
        if (
            response is None
            and settings.identity_header
            and not (is_api or path == "/healthz" or path.startswith("/static/"))
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
        response.headers["X-Content-Type-Options"] = "nosniff"
        if path == "/access" or path.startswith("/access/"):
            response.headers["Cache-Control"] = "no-store"
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Content-Security-Policy"] = CSP
        return response

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
