import logging
import smtplib
import ssl
from collections.abc import Mapping
from email.message import EmailMessage
from urllib.parse import quote, unquote, urlsplit

import anyio
import httpx

from verifair.config import Settings, one_address

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
MAIL_THREADS = anyio.CapacityLimiter(2)


def _cut(text: str) -> str:
    if len(text) <= JUDGE_THIS_MAX:
        return text
    head = text[: JUDGE_THIS_MAX + 1]
    end = max(head.rfind(" "), head.rfind("\n"))
    return text[: end if end > 0 else JUDGE_THIS_MAX].rstrip() + "\nRead the rest on the page."


def review_email(
    report: Mapping,
    version: int,
    judge: str,
    base_url: str,
    *,
    signer: str,
    asker: str | None = None,
    added: bool = False,
) -> tuple[str, str]:
    title = " ".join(report["title"].split())
    if added:
        opening = f'{asker} asked you to review "{title}" (v{version}, signed by {signer}).'
    else:
        by = f" by {asker}" if asker else ""
        opening = f'{signer} signed v{version} of "{title}". You were asked to review it{by}.'
    blocks = [opening, f"Project: {report['project']} · Risk: {report['risk']}"]
    if judge:
        blocks.append(f"Judge this:\n{_cut(judge)}")
    blocks += [
        f"Open it to comment, approve or request changes:\n{base_url}/r/{quote(report['id'])}",
        "— Sent by verifair. Replies to this mail are not read.",
    ]
    return f'Review requested: "{title}"', "\n\n".join(blocks)


def _failed(report_id: str, recipients: int, exc: Exception) -> None:
    log.warning(
        "email failed report=%s recipients=%d error=%s code=%s",
        report_id,
        recipients,
        type(exc).__name__,
        getattr(exc, "smtp_code", "-"),
    )


def send_email(settings: Settings, report_id: str, subject: str, letters: dict[str, str]) -> None:
    letters = {to: text for to, text in letters.items() if one_address(to)}
    if not settings.smtp_url or not letters:
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
            secure = url.scheme == "smtps"
            if not secure and server.has_extn("starttls"):
                server.starttls(context=tls)
                secure = True
            if url.username and not secure:
                log.warning(
                    "email not sent report=%s recipients=%d: the server offers no TLS,"
                    " so verifair does not send the SMTP password",
                    report_id,
                    len(letters),
                )
                return
            if url.username:
                server.login(unquote(url.username), unquote(url.password or ""))
            for to, text in letters.items():
                message = EmailMessage()
                message["Subject"] = subject
                message["From"] = settings.email_from
                message["To"] = to
                message.set_content(text)
                try:
                    server.send_message(message, to_addrs=[to])
                except (smtplib.SMTPRecipientsRefused, smtplib.SMTPResponseException) as exc:
                    _failed(report_id, 1, exc)
    except Exception as exc:
        _failed(report_id, len(letters), exc)


async def email(settings: Settings, report_id: str, subject: str, letters: dict[str, str]) -> None:
    # a slow mail server must not take threads from the shared pool that serves requests
    await anyio.to_thread.run_sync(
        send_email, settings, report_id, subject, letters, limiter=MAIL_THREADS
    )
