import os
from dataclasses import dataclass, field
from email.utils import getaddresses
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    api_token: str | None = field(repr=False)
    database_url: str
    base_url: str
    slack_webhook_url: str | None
    required_sections: tuple[str, ...] = ()
    bootstrap_admin_id: str | None = None
    db_pool_size: int = 10
    smtp_url: str | None = field(default=None, repr=False)
    email_from: str | None = None
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = field(default=None, repr=False)
    session_secret: str | None = field(default=None, repr=False)
    oidc_allowed_domains: tuple[str, ...] = ()

    @property
    def shared(self) -> bool:
        return self.oidc_issuer is not None


GOOGLE = "https://accounts.google.com"
DEFAULT_REQUIRED_SECTIONS = "Verification,Rollback,Security impact"


def _split(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def one_address(value: str) -> bool:
    return "@" in value and getaddresses([value]) == [("", value)]


def https_or_local(url) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    return bool(host) and (
        parts.scheme == "https" or (parts.scheme == "http" and host in ("localhost", "127.0.0.1"))
    )


def check_oidc(settings: Settings) -> None:
    if not settings.shared:
        return
    required = {
        "VERIFAIR_OIDC_CLIENT_ID": settings.oidc_client_id,
        "VERIFAIR_OIDC_CLIENT_SECRET": settings.oidc_client_secret,
        "VERIFAIR_SESSION_SECRET": settings.session_secret,
    }
    if missing := [name for name, value in required.items() if not value]:
        verb = "is" if len(missing) == 1 else "are"
        raise RuntimeError(
            f"{', '.join(missing)} {verb} required when VERIFAIR_OIDC_ISSUER is set."
        )
    if len(settings.session_secret) < 32:
        raise RuntimeError(
            "VERIFAIR_SESSION_SECRET must be at least 32 characters."
            " Create one with: openssl rand -base64 48"
        )
    if not https_or_local(settings.oidc_issuer):
        raise RuntimeError(
            "VERIFAIR_OIDC_ISSUER must be an https URL such as https://accounts.google.com"
        )
    if settings.oidc_issuer.removesuffix("/") == GOOGLE and not settings.oidc_allowed_domains:
        raise RuntimeError(
            "VERIFAIR_OIDC_ALLOWED_DOMAINS is required with Google sign-in. Without it a"
            " personal Google account made for a member's email could sign in as them."
            " Set it to your Google Workspace domain, such as example.com."
        )
    if not https_or_local(settings.base_url):
        raise RuntimeError(
            "VERIFAIR_BASE_URL must be the https URL people open, such as"
            " https://verifair.example.com. Sign-in needs it for the redirect URI."
        )


def _valid_smtp_url(url: str) -> bool:
    parts = urlsplit(url)
    try:
        return parts.scheme in ("smtp", "smtps") and bool(parts.hostname) and parts.port != 0
    except ValueError:
        return False


def load_settings() -> Settings:
    if os.environ.get("VERIFAIR_IDENTITY_HEADER"):
        raise RuntimeError(
            "VERIFAIR_IDENTITY_HEADER was removed. verifair now signs people in itself:"
            " set VERIFAIR_OIDC_ISSUER, VERIFAIR_OIDC_CLIENT_ID, VERIFAIR_OIDC_CLIENT_SECRET"
            " and VERIFAIR_SESSION_SECRET instead. See docs/access.md."
        )
    api_token = os.environ.get("VERIFAIR_API_TOKEN") or None
    oidc_issuer = os.environ.get("VERIFAIR_OIDC_ISSUER", "").strip() or None
    if not api_token and not oidc_issuer:
        raise RuntimeError(
            "VERIFAIR_API_TOKEN is required in local mode."
            " Set VERIFAIR_OIDC_ISSUER for shared mode."
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
        bootstrap_admin_id=os.environ.get("VERIFAIR_BOOTSTRAP_ADMIN_ID", "").strip() or None,
        db_pool_size=int(pool_size),
        smtp_url=smtp_url,
        email_from=email_from,
        oidc_issuer=oidc_issuer,
        oidc_client_id=os.environ.get("VERIFAIR_OIDC_CLIENT_ID", "").strip() or None,
        oidc_client_secret=os.environ.get("VERIFAIR_OIDC_CLIENT_SECRET", "").strip() or None,
        session_secret=os.environ.get("VERIFAIR_SESSION_SECRET", "").strip() or None,
        oidc_allowed_domains=tuple(
            d.lower() for d in _split(os.environ.get("VERIFAIR_OIDC_ALLOWED_DOMAINS", ""))
        ),
    )
