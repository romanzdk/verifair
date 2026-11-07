import logging
import smtplib
import ssl
from collections.abc import Mapping
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib.parse import quote, unquote, urlsplit

import anyio
import httpx

from verifair.config import Settings, one_address

log = logging.getLogger("verifair")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _revision(report: Mapping, version: int, target: str) -> str:
    if report.get("workflow") == "plan_first":
        return f"plan revision {version}" if target == "plan" else f"implementation v{version}"
    return f"v{version}"


# the excerpt comes from an agent's report, so readers must not take it as verifair's own words
JUDGE_HEADING = "Judge this (written by the agent, not checked by verifair):"


def _tab(report: Mapping, target: str) -> str:
    return f"?tab={target}" if report.get("workflow") == "plan_first" else ""


def report_pushed_text(
    report: Mapping, version: int, base_url: str, *, target: str = "implementation"
) -> str:
    revision = _revision(report, version, target)
    if report.get("workflow") != "plan_first":
        revision = f"report {revision}"
    return (
        f"Draft {revision} in {_esc(report['project'])}: {_esc(report['title'])} "
        f"{base_url}/r/{report['id']}{_tab(report, target)}"
    )


def ready_for_review_text(
    report: Mapping,
    version: int,
    signer_name: str,
    base_url: str,
    *,
    target: str = "implementation",
) -> str:
    return (
        f"Ready for review: {_esc(report['title'])} {_revision(report, version, target)} "
        f"(risk {report['risk']}), signed by {_esc(signer_name)} "
        f"{base_url}/r/{report['id']}{_tab(report, target)}"
    )


def comment_text(report: Mapping, comment: Mapping, base_url: str) -> str:
    section = _esc(comment["section"] or "report")
    target = comment.get("target", "implementation")
    revision = (
        f" {_revision(report, comment['version'], target)}"
        if report.get("workflow") == "plan_first"
        else ""
    )
    url = f"{base_url}/r/{report['id']}{_tab(report, target)}#vf_c{comment['id']}"
    agent = " (agent)" if comment.get("author_id") == "agent" else ""
    return (
        f"{_esc(comment['author_name'])}{agent} commented on {_esc(report['title'])}{revision} "
        f"› {section}: {_esc(comment['body'][:140])} {url}"
    )


def verdict_text(report: Mapping, verdict: Mapping, base_url: str) -> str:
    action = "approved" if verdict["verdict"] == "approve" else "requested changes on"
    target = verdict.get("target", "implementation")
    return (
        f"{_esc(verdict['reviewer_name'])} {action} {_esc(report['title'])} "
        f"{_revision(report, verdict['version'], target)}: {_esc(verdict['summary'])} "
        f"{base_url}/r/{report['id']}{_tab(report, target)}"
    )


def withdrawn_text(report: Mapping, name: str, version: int, target: str, base_url: str) -> str:
    return (
        f"{_esc(name)} withdrew their verdict on {_esc(report['title'])} "
        f"{_revision(report, version, target)} "
        f"{base_url}/r/{report['id']}{_tab(report, target)}"
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


def welcome_email(added_by: str, base_url: str) -> tuple[str, str]:
    text = "\n\n".join(
        [
            f"{added_by} added you to verifair, where coding agents publish change reports"
            " for people to sign and review.",
            f"Sign in with your work account:\n{base_url}/",
            "To connect your coding agent, create a token and install the verifair skill:\n"
            f"{base_url}/access/tokens",
            "— Sent by verifair. Replies to this mail are not read.",
        ]
    )
    return "You were added to verifair", text


def review_email(
    report: Mapping,
    version: int,
    judge: str,
    base_url: str,
    *,
    signer: str | None,
    asker: str | None = None,
    added: bool = False,
    target: str = "implementation",
) -> tuple[str, str]:
    title = " ".join(report["title"].split())
    revision = _revision(report, version, target)
    if added:
        signed = f", signed by {signer}" if signer else ""
        opening = f'{asker} asked you to review "{title}" ({revision}{signed}).'
    else:
        by = f" by {asker}" if asker else ""
        opening = f'{signer} signed {revision} of "{title}". You were asked to review it{by}.'
    blocks = [opening, f"Project: {report['project']} · Risk: {report['risk']}"]
    if judge:
        blocks.append(f"{JUDGE_HEADING}\n{_cut(judge)}")
    blocks += [
        "Open it to comment, approve or request changes:\n"
        f"{base_url}/r/{quote(report['id'])}{_tab(report, target)}",
        "— Sent by verifair. Replies to this mail are not read.",
    ]
    subject = f'Review requested: "{title}"'
    if report.get("workflow") == "plan_first":
        subject += f" ({revision})"
    return subject, "\n\n".join(blocks)


def _failed(report_id: str, recipients: int, exc: Exception) -> None:
    log.warning(
        "email failed report=%s recipients=%d error=%s code=%s",
        report_id,
        recipients,
        type(exc).__name__,
        getattr(exc, "smtp_code", "-"),
    )


def send_email(
    settings: Settings, report_id: str, subject: str, letters: dict[str, str]
) -> list[str]:
    letters = {to: text for to, text in letters.items() if one_address(to)}
    delivered: list[str] = []
    refused: list[str] = []
    if not settings.smtp_url or not letters:
        return delivered
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
                return delivered
            if url.username:
                server.login(unquote(url.username), unquote(url.password or ""))
            for to, text in letters.items():
                message = EmailMessage()
                message["Subject"] = subject
                message["From"] = settings.email_from
                message["To"] = to
                message["Date"] = formatdate(usegmt=True)
                message["Message-ID"] = make_msgid(domain=settings.email_from.rpartition("@")[2])
                message["Auto-Submitted"] = "auto-generated"
                message.set_content(text)
                try:
                    server.send_message(message, to_addrs=[to])
                except smtplib.SMTPServerDisconnected:
                    raise
                except smtplib.SMTPException as exc:
                    refused.append(to)
                    _failed(report_id, 1, exc)
                else:
                    delivered.append(to)
    except Exception as exc:
        if missed := len(letters) - len(delivered) - len(refused):
            _failed(report_id, missed, exc)
    if delivered:
        log.info("email sent report=%s recipients=%d", report_id, len(delivered))
    return delivered


async def email(
    settings: Settings, report_id: str, subject: str, letters: dict[str, str]
) -> list[str]:
    # a slow mail server must not take threads from the shared pool that serves requests
    return await anyio.to_thread.run_sync(
        send_email, settings, report_id, subject, letters, limiter=MAIL_THREADS
    )
