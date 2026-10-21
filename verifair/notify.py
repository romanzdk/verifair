import logging
from collections.abc import Mapping

import httpx

from verifair.config import Settings

log = logging.getLogger("verifair")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def report_pushed_text(report: Mapping, version: int, base_url: str) -> str:
    return (
        f"New report v{version} in {_esc(report['project'])}: {_esc(report['title'])} "
        f"(risk {report['risk']}) {base_url}/r/{report['id']}"
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
