from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from verifair import api, db, web
from verifair.config import Settings

CSP = "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'"


def _cross_origin(request: Request) -> bool:
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None:
        return site not in ("same-origin", "none")
    origin = request.headers.get("Origin")
    return origin is not None and urlsplit(origin).netloc != request.headers.get("Host")


def create_app(settings: Settings) -> FastAPI:
    conn = db.connect(settings.db_path)
    try:
        db.init_schema(conn)
    finally:
        conn.close()

    app = FastAPI(title="verifair")
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(api.router)
    app.include_router(web.router)

    @app.middleware("http")
    async def web_guard(request: Request, call_next):
        is_api = request.url.path.startswith("/api/")
        if request.method == "POST" and not is_api and _cross_origin(request):
            return PlainTextResponse("cross-origin request refused", 403)
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Content-Security-Policy"] = CSP
        return response

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
