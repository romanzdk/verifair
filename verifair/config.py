import os
from dataclasses import dataclass
from email.utils import getaddresses
from urllib.parse import urlsplit


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
    smtp_url: str | None = None
    email_from: str | None = None


DEFAULT_REQUIRED_SECTIONS = "Verification,Rollback,Security impact"


def _split(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def one_address(value: str) -> bool:
    return "@" in value and getaddresses([value]) == [("", value)]


def _valid_smtp_url(url: str) -> bool:
    parts = urlsplit(url)
    try:
        return parts.scheme in ("smtp", "smtps") and bool(parts.hostname) and parts.port != 0
    except ValueError:
        return False


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
    pool_size = os.environ.get("VERIFAIR_DB_POOL_SIZE", "10")
    if not pool_size.isdecimal() or int(pool_size) < 1:
        raise RuntimeError("VERIFAIR_DB_POOL_SIZE must be a whole number of 1 or more.")
    smtp_url = os.environ.get("VERIFAIR_SMTP_URL") or None
    email_from = os.environ.get("VERIFAIR_EMAIL_FROM", "").strip() or None
    if smtp_url and not _valid_smtp_url(smtp_url):
        raise RuntimeError(
            "VERIFAIR_SMTP_URL must look like smtp://user:password@host:587"
            " or smtps://user:password@host:465"
        )
    if smtp_url and not email_from:
        raise RuntimeError("VERIFAIR_EMAIL_FROM is required when VERIFAIR_SMTP_URL is set.")
    if email_from and not one_address(email_from):
        raise RuntimeError(
            "VERIFAIR_EMAIL_FROM must be one plain address such as verifair@example.com."
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
        db_pool_size=int(pool_size),
        smtp_url=smtp_url,
        email_from=email_from,
    )
