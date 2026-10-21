from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from verifair import api, db, web
from verifair.config import Settings

CSP = (
    "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline';"
    " frame-ancestors 'none'"
)
SIGN_IN = "Sign in through your company login to use verifair."


def _active_member(db_path: str, user_id: str):
    conn = db.connect(db_path)
    try:
        member = db.get_user(conn, user_id)
    finally:
        conn.close()
    return member if member is not None and member["active"] else None


async def _membership_denial(request: Request, settings: Settings):
    user_id = request.headers.get(settings.identity_header, "").strip()
    if not user_id or user_id == "agent":
        message, status = SIGN_IN, 401
    else:
        request.state.member = await run_in_threadpool(_active_member, settings.db_path, user_id)
        if request.state.member is not None:
            return None
        message, status = web.NOT_MEMBER.format(id=user_id), 403
    return web.templates.TemplateResponse(
        request, "denied.html", {"message": message}, status_code=status
    )


def create_app(settings: Settings) -> FastAPI:
    admin_id = settings.bootstrap_admin_id
    if settings.identity_header and admin_id and not web.plain_id(admin_id):
        raise RuntimeError(
            "VERIFAIR_BOOTSTRAP_ADMIN_ID must be a plain id such as jane@example.com"
        )
    conn = db.connect(settings.db_path)
    try:
        db.init_schema(conn)
        if settings.identity_header:
            if settings.bootstrap_admin_id:
                db.bootstrap_admin(conn, settings.bootstrap_admin_id)
            if not db.list_users(conn):
                raise RuntimeError(
                    "VERIFAIR_BOOTSTRAP_ADMIN_ID is required to create the first admin"
                )
    finally:
        conn.close()

    app = FastAPI(title="verifair")
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(api.router)
    app.include_router(web.router)

    @app.middleware("http")
    async def web_guard(request: Request, call_next):
        path = request.url.path
        is_api = path.startswith("/api/")
        request.state.member = None
        response = None
        if settings.identity_header and not (
            is_api or path == "/healthz" or path.startswith("/static/")
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
