from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from verifair import api, db, web
from verifair.config import Settings


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

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
