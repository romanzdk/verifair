import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    api_token: str
    db_path: str
    base_url: str
    slack_webhook_url: str | None
    required_sections: tuple[str, ...] = ()
    identity_header: str | None = None


DEFAULT_REQUIRED_SECTIONS = "Verification,Rollback,Security impact"


def _split(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def load_settings() -> Settings:
    api_token = os.environ.get("VERIFAIR_API_TOKEN")
    if not api_token:
        raise RuntimeError("VERIFAIR_API_TOKEN is required")
    return Settings(
        api_token=api_token,
        db_path=os.environ.get("VERIFAIR_DB_PATH", "./verifair.db"),
        base_url=os.environ.get("VERIFAIR_BASE_URL", "http://localhost:8000").rstrip("/"),
        slack_webhook_url=os.environ.get("VERIFAIR_SLACK_WEBHOOK_URL") or None,
        required_sections=_split(
            os.environ.get("VERIFAIR_REQUIRED_SECTIONS", DEFAULT_REQUIRED_SECTIONS)
        ),
        identity_header=os.environ.get("VERIFAIR_IDENTITY_HEADER") or None,
    )
