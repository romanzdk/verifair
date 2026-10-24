import logging
import smtplib
import ssl
from collections.abc import Mapping
from email.message import EmailMessage
from urllib.parse import quote, unquote, urlsplit

import httpx

from verifair.config import Settings

log = logging.getLogger("verifair")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def report_pushed_text(report: Mapping, version: int, base_url: str) -> str:
    return (
        f"Draft report v{version} in {_esc(report['project'])}: {_esc(report['title'])} "
        f"{base_url}/r/{report['id']}"
    )


def ready_for_review_text(report: Mapping, version: int, signer_name: str, base_url: str) -> str:
    return (
        f"Ready for review: {_esc(report['title'])} v{version} (risk {report['risk']}), "
        f"signed by {_esc(signer_name)} {base_url}/r/{report['id']}"
    )


def comment_text(report: Mapping, comment: Mapping, base_url: str) -> str:
    section = _esc(comment["section"] or "report")
    url = f"{base_url}/r/{report['id']}#vf_c{comment['id']}"
    return (
        f"{_esc(comment['author_name'])} commented on {_esc(report['title'])} › {section}: "
        f"{_esc(comment['body'][:140])} {url}"
    )


def verdict_text(report: Mapping, verdict: Mapping, base_url: str) -> str:
    action = "approved" if verdict["verdict"] == "approve" else "requested changes on"
    return (
        f"{_esc(verdict['reviewer_name'])} {action} {_esc(report['title'])} "
        f"v{verdict['version']}: {_esc(verdict['summary'])} {base_url}/r/{report['id']}"
    )


def send(settings: Settings, text: str) -> None:
    if not settings.slack_webhook_url:
        return
    try:
        httpx.post(settings.slack_webhook_url, json={"text": text}, timeout=5).raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "")
        log.warning("slack notification failed: %s %s", type(exc).__name__, status)


JUDGE_THIS_MAX = 1500


def review_email(report: Mapping, asker: str, judge: str, base_url: str) -> tuple[str, str]:
    lines = [
        f"{asker} asked you to review {report['title']}.",
        f"Project: {report['project']}",
        f"Risk: {report['risk']}",
    ]
    if judge:
        trimmed = judge[:JUDGE_THIS_MAX] + ("…" if len(judge) > JUDGE_THIS_MAX else "")
        lines += ["", "Judge this:", trimmed]
    lines += ["", f"{base_url}/r/{quote(report['id'])}"]
    subject = " ".join(f"Review requested: {report['title']}".split())
    return subject, "\n".join(lines)


def email(settings: Settings, recipients: list[str], subject: str, text: str) -> None:
    if not settings.smtp_url or not recipients:
        return
    url = urlsplit(settings.smtp_url)
    # smtplib does not check certificates unless it gets a default context
    tls = ssl.create_default_context()
    try:
        if url.scheme == "smtps":
            server = smtplib.SMTP_SSL(url.hostname, url.port or 0, timeout=10, context=tls)
        else:
            server = smtplib.SMTP(url.hostname, url.port or 0, timeout=10)
        with server:
            server.ehlo()
            if url.scheme == "smtp" and server.has_extn("starttls"):
                server.starttls(context=tls)
            if url.username:
                server.login(unquote(url.username), unquote(url.password or ""))
            for to in recipients:
                message = EmailMessage()
                message["Subject"], message["From"], message["To"] = (
                    subject,
                    settings.email_from,
                    to,
                )
                message.set_content(text)
                server.send_message(message)
    except Exception as exc:
        code = f" {exc.smtp_code}" if isinstance(exc, smtplib.SMTPResponseException) else ""
        log.warning("email failed: %s%s", type(exc).__name__, code)
