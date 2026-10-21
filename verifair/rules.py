import sqlite3
from dataclasses import dataclass

from verifair.render import Thread

STATUS_LABELS = {
    "awaiting_review": "Awaiting review",
    "changes_requested": "Changes requested",
    "approved": "Approved",
}

_VERDICT_LABELS = {"approve": "approved", "request_changes": "requested changes"}


@dataclass
class Reviewer:
    id: str
    name: str
    verdict: str | None
    verdict_version: int | None
    label: str
    summary: str = ""


def _by_id(row: sqlite3.Row) -> int:
    return row["id"]


def latest_verdicts(verdicts: list[sqlite3.Row], version: int) -> dict[str, sqlite3.Row]:
    latest: dict[str, sqlite3.Row] = {}
    for row in sorted(verdicts, key=_by_id):
        if row["version"] == version:
            latest[row["reviewer_id"]] = row
    return latest


def compute_status(verdicts: list[sqlite3.Row], current_version: int) -> str:
    kinds = {row["verdict"] for row in latest_verdicts(verdicts, current_version).values()}
    if "request_changes" in kinds:
        return "changes_requested"
    if "approve" in kinds:
        return "approved"
    return "awaiting_review"


def reviewers(comments: list[sqlite3.Row], verdicts: list[sqlite3.Row]) -> list[Reviewer]:
    names: dict[str, str] = {}
    latest: dict[str, sqlite3.Row] = {}
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
    return sorted(result, key=lambda r: r.name)


def can_request_changes(threads: list[Thread], reviewer_id: str, summary: str) -> bool:
    return bool(summary.strip()) or any(
        t.root["resolved_at"] is None and t.root["author_id"] == reviewer_id for t in threads
    )
