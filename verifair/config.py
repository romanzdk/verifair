import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    api_token: str | None
    database_url: str
    base_url: str
    slack_webhook_url: str | None
    required_sections: tuple[str, ...] = ()
    identity_header: str | None = None
    bootstrap_admin_id: str | None = None
    db_pool_size: int = 10


DEFAULT_REQUIRED_SECTIONS = "Verification,Rollback,Security impact"


def _split(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def load_settings() -> Settings:
    api_token = os.environ.get("VERIFAIR_API_TOKEN") or None
    identity_header = os.environ.get("VERIFAIR_IDENTITY_HEADER") or None
    if not api_token and not identity_header:
        raise RuntimeError(
            "VERIFAIR_API_TOKEN is required in local mode."
            " Set VERIFAIR_IDENTITY_HEADER for shared mode."
        )
    database_url = os.environ.get("VERIFAIR_DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "VERIFAIR_DATABASE_URL is required,"
            " for example postgresql://verifair:verifair@localhost:5432/verifair"
        )
    return Settings(
        api_token=api_token,
        database_url=database_url,
        base_url=os.environ.get("VERIFAIR_BASE_URL", "http://localhost:8000").rstrip("/"),
        slack_webhook_url=os.environ.get("VERIFAIR_SLACK_WEBHOOK_URL") or None,
        required_sections=_split(
            os.environ.get("VERIFAIR_REQUIRED_SECTIONS", DEFAULT_REQUIRED_SECTIONS)
        ),
        identity_header=identity_header,
        bootstrap_admin_id=os.environ.get("VERIFAIR_BOOTSTRAP_ADMIN_ID", "").strip() or None,
        db_pool_size=int(os.environ.get("VERIFAIR_DB_POOL_SIZE", "10")),
    )
