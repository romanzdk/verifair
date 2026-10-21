from fastapi import FastAPI

from verifair import api, db
from verifair.config import Settings


def create_app(settings: Settings) -> FastAPI:
    conn = db.connect(settings.db_path)
    try:
        db.init_schema(conn)
    finally:
        conn.close()

    app = FastAPI(title="verifair")
    app.state.settings = settings
    app.include_router(api.router)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
