from fastapi import FastAPI

from verifair.config import Settings


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="verifair")
    app.state.settings = settings

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app
