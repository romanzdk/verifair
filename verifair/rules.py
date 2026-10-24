from dataclasses import dataclass

from verifair.db import Row
from verifair.render import Thread

STATUS_LABELS = {
    "unsigned": "Unsigned",
    "awaiting_review": "Awaiting review",
    "changes_requested": "Changes requested",
    "approved": "Approved",
}

MODERATION = (
    "Only the person who started this thread, the report owner for agent threads, or an admin"
    " can do that."
)
NO_OWNER = "This report has no owner. Push it again with an agent token under a new id."

_VERDICT_LABELS = {"approve": "approved", "request_changes": "requested changes"}


@dataclass
class Reviewer:
    id: str
    name: str
    verdict: str | None
    verdict_version: int | None
    label: str
    summary: str = ""


def _by_id(row: Row) -> int:
    return row["id"]


def latest_verdicts(verdicts: list[Row], version: int) -> dict[str, Row]:
    latest: dict[str, Row] = {}
    for row in sorted(verdicts, key=_by_id):
        if row["version"] == version:
            latest[row["reviewer_id"]] = row
    return latest


def compute_status(
    verdicts: list[Row], current_version: int, signed: bool, signer_id: str | None = None
) -> str:
    if not signed:
        return "unsigned"
    kinds = {
        row["verdict"]
        for reviewer_id, row in latest_verdicts(verdicts, current_version).items()
        if reviewer_id != signer_id
    }
    if "request_changes" in kinds:
        return "changes_requested"
    if "approve" in kinds:
        return "approved"
    return "awaiting_review"


def reviewers(
    comments: list[Row], verdicts: list[Row], requested: dict[str, str] | None = None
) -> list[Reviewer]:
    names: dict[str, str] = {}
    latest: dict[str, Row] = {}
    rows = [(r, r["author_id"], r["author_name"]) for r in comments]
    rows += [(r, r["reviewer_id"], r["reviewer_name"]) for r in verdicts]
    # ids are per table, so only the timestamp can order a comment against a verdict
    for _, person_id, name in sorted(rows, key=lambda item: item[0]["created_at"]):
        if person_id != "agent":
            names[person_id] = name
    for row in sorted(verdicts, key=_by_id):
        latest[row["reviewer_id"]] = row
    result = []
    for person_id, name in names.items():
        row = latest.get(person_id)
        if row is None:
            result.append(Reviewer(person_id, name, None, None, "reviewing"))
        else:
            label = f"{_VERDICT_LABELS[row['verdict']]} v{row['version']}"
            result.append(
                Reviewer(person_id, name, row["verdict"], row["version"], label, row["summary"])
            )
    for person_id, name in (requested or {}).items():
        if person_id not in names:
            result.append(Reviewer(person_id, name, None, None, "review requested"))
    return sorted(result, key=lambda r: r.name)


def open_blocking(threads: list[Thread]) -> list[Thread]:
    return [t for t in threads if t.blocking and t.root["resolved_at"] is None]


def can_approve(threads: list[Thread]) -> bool:
    return not open_blocking(threads)


def can_request_changes(threads: list[Thread], reviewer_id: str, summary: str) -> bool:
    return bool(summary.strip()) or any(
        t.root["author_id"] == reviewer_id for t in open_blocking(threads)
    )


def can_moderate(
    *,
    shared: bool,
    user_id: str,
    role: str | None,
    root_author_id: str,
    report_author_id: str | None,
) -> bool:
    return (
        not shared
        or user_id == root_author_id
        or role == "admin"
        or (root_author_id == "agent" and user_id == report_author_id)
    )


RISK_RANK = {"high": 0, "medium": 1, "low": 2}


def _newest_first(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: row["report"]["updated_at"], reverse=True)


def inbox(
    rows: list[dict], *, user_id: str, mine: set[str], requested: set[str]
) -> dict[str, list[dict]]:
    def is_mine(row: dict, status: str) -> bool:
        return row["report"]["id"] in mine and row["status"] == status

    review = [
        row
        for row in rows
        if row["report"]["id"] in requested
        and row["report"]["id"] not in mine
        and row["status"] == "awaiting_review"
        and user_id not in row["reviewed_by"]
        and row["report"]["signer_id"] != user_id
    ]
    return {
        "review": sorted(
            review, key=lambda row: (RISK_RANK[row["report"]["risk"]], row["signed_at"])
        ),
        "sign": _newest_first([row for row in rows if is_mine(row, "unsigned")]),
        "returned": _newest_first([row for row in rows if is_mine(row, "changes_requested")]),
        "waiting": _newest_first([row for row in rows if is_mine(row, "awaiting_review")]),
    }
