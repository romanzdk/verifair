"""Check that the new code keeps every review the previous release wrote.

Usage, with VERIFAIR_URL and VERIFAIR_API_TOKEN set:
  python3 scripts/upgrade-check.py seed OLD_DEMO.py                   # old release, after its demo
  python3 scripts/upgrade-check.py snapshot OLD_DEMO.py > before.json # old release running
  python3 scripts/upgrade-check.py compare OLD_DEMO.py before.json    # new code, same database

OLD_DEMO.py is the previous release's scripts/demo.py: its REPORTS name the reviews to read.
`seed` adds comment threads, which the demo does not have, to the first review.
`compare` fails when anything the old release returned is missing or different. New fields are fine.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("VERIFAIR_URL", "http://localhost:8000").rstrip("/")


def request(path: str, method: str = "GET", payload: dict | None = None):
    token = os.environ["VERIFAIR_API_TOKEN"]
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method, headers=headers)
    return urllib.request.urlopen(req, timeout=30)


def get(path: str) -> dict:
    with request(path) as response:
        return json.load(response)


def report_ids(demo_path: str) -> list[str]:
    spec = importlib.util.spec_from_file_location("old_demo", demo_path)
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    return list(dict.fromkeys(report["id"] for report in demo.REPORTS))


def seed(id: str) -> None:
    for comment in (
        {"body": "Upgrade check: a general comment."},
        {"body": "On Intent.", "section": "Intent", "blocking": False},
    ):
        with request(f"/api/reports/{id}/comments", "POST", comment) as response:
            root = json.load(response)["id"]
        reply = {"body": "Upgrade check: a reply.", "parent_id": root}
        request(f"/api/reports/{id}/comments", "POST", reply).close()


def snapshot(ids: list[str]) -> dict:
    reviews = {}
    for id in ids:
        report = get(f"/api/reports/{id}")
        versions = [
            get(f"/api/reports/{id}/versions/{n}") for n in range(1, report["current_version"] + 1)
        ]
        comments = {}
        for target in ("implementation", "plan"):
            try:
                comments[target] = get(f"/api/reports/{id}/comments?target={target}")
            except urllib.error.HTTPError:
                pass
        reviews[id] = {"report": report, "versions": versions, "comments": comments}
    return reviews


def changed(before: object, after: object, path: str = "") -> list[str]:
    if isinstance(before, dict):
        if not isinstance(after, dict):
            return [f"{path}: {before!r} -> {after!r}"]
        problems = []
        for key, value in before.items():
            where = f"{path}.{key}" if path else key
            if key not in after:
                problems.append(f"{where}: missing")
            else:
                problems += changed(value, after[key], where)
        return problems
    if isinstance(before, list):
        if not isinstance(after, list) or len(after) != len(before):
            size = len(after) if isinstance(after, list) else repr(after)
            return [f"{path}: {len(before)} items -> {size}"]
        return [
            problem
            for i, (old, new) in enumerate(zip(before, after))
            for problem in changed(old, new, f"{path}[{i}]")
        ]
    return [] if before == after else [f"{path}: {before!r} -> {after!r}"]


def compare(ids: list[str], before: dict) -> list[str]:
    problems = changed(before, snapshot(ids))
    for id in ids:
        for path, method in (
            (f"/r/{id}", "GET"),
            (f"/api/reports/{id}/archive", "POST"),
            (f"/api/reports/{id}/restore", "POST"),
        ):
            try:
                request(path, method).close()
            except urllib.error.HTTPError as error:
                problems.append(f"{method} {path}: HTTP {error.code}")
    return problems + [f"after archive and restore: {p}" for p in changed(before, snapshot(ids))]


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] not in ("seed", "snapshot", "compare"):
        sys.exit(__doc__)
    ids = report_ids(sys.argv[2])
    if sys.argv[1] == "seed":
        seed(ids[0])
        return
    if sys.argv[1] == "snapshot":
        json.dump(snapshot(ids), sys.stdout, indent=1)
        return
    with open(sys.argv[3], encoding="utf-8") as file:
        problems = compare(ids, json.load(file))
    for problem in problems:
        print(problem)
    if problems:
        sys.exit(f"{len(problems)} differences after the upgrade")
    print(f"{len(ids)} reviews read the same after the upgrade")


if __name__ == "__main__":
    main()
