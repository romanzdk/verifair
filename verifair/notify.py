import logging
from collections.abc import Mapping

import httpx

from verifair.config import Settings

log = logging.getLogger("verifair")


def report_pushed_text(report: Mapping, version: int, base_url: str) -> str:
    return (
        f"New report v{version} in {report['project']}: {report['title']} "
        f"(risk {report['risk']}) {base_url}/r/{report['id']}"
    )


def comment_text(report: Mapping, comment: Mapping, base_url: str) -> str:
    section = comment["section"] or "report"
    url = f"{base_url}/r/{report['id']}#c{comment['id']}"
    return (
        f"{comment['author_name']} commented on {report['title']} › {section}: "
        f"{comment['body'][:140]} {url}"
    )


def verdict_text(report: Mapping, verdict: Mapping, base_url: str) -> str:
    action = "approved" if verdict["verdict"] == "approve" else "requested changes on"
    return (
        f"{verdict['reviewer_name']} {action} {report['title']} v{verdict['version']}: "
        f"{verdict['summary']} {base_url}/r/{report['id']}"
    )


def send(settings: Settings, text: str) -> None:
    if not settings.slack_webhook_url:
        return
    try:
        httpx.post(settings.slack_webhook_url, json={"text": text}, timeout=5).raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("slack notification failed: %s", exc)
