"""Load demo reviews into a local verifair through its API.

Usage: VERIFAIR_API_TOKEN=change-me python3 scripts/demo.py
"""

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

URL = os.environ.get("VERIFAIR_URL", "http://localhost:8000").rstrip("/")
TEMPLATE = Path(__file__).resolve().parents[1] / "docs" / "report-template.md"


def body(intent, judge, design, edges, verification, not_verified, rollback, security, reviews):
    return f"""## Intent
{intent}

## Design
{design}

## Edge cases
| Case | How it is handled | Proved by |
|---|---|---|
{edges}

## Verification
| Proves | Command | Result | Link | Fails on revert |
|---|---|---|---|---|
{verification}

## Not verified / assumptions
{not_verified}

## Rollback
{rollback}

## Security impact
{security}

## Agent reviews
| Round | Reviewers | Findings | Fixed | Dismissed |
|---|---|---|---|---|
{reviews}

## Judge this
{judge}

## Changes
| # | Repo | Merge request | What it changes | Status |
|---|---|---|---|---|
| 1 | demo | local only | the change described above | open |
"""


ROUNDING = body(
    "Amounts are always rounded to 2 decimals, which is wrong for JPY (0) and BHD (3)."
    " Round to each currency's minor units.",
    "- JPY invoice totals can still drift by one yen, because lines are rounded before summing."
    " Is that fine until the follow-up?",
    "`round_amount(amount, currency)` reads minor units from the currency table."
    " All three callers pass the currency.\n\n"
    "| | 1234.6 JPY | 1.2346 BHD |\n|---|---|---|\n"
    "| Before | 1234.60 | 1.23 |\n| After | 1235 | 1.235 |\n\n"
    "```mermaid\nflowchart LR\n"
    "  A[Amount] --> B[Look up currency] --> C[Round to its minor units]\n```\n\n"
    "Rejected: rounding only the total. It changes every stored line amount.",
    "| JPY | 0 decimals | `test_jpy_rounding` |\n"
    "| BHD | 3 decimals | `test_bhd_rounding` |\n"
    "| Unknown currency | raises before anything is stored | `test_unknown_currency` |",
    "| Rounding per currency | `pytest -q` | ✅ 212 passed | Execution tab | yes |",
    "- JPY totals can drift by one minor unit (found by the review sub-agent, see Execution).",
    "Revert the commit. No data changes.",
    "None.",
    "| 1 | Python, quant | 2 | 1 | 1 |\n| 2 | Python, quant | 0 | 0 | 0 |",
)


def event(n, kind, title, start, end, **fields):
    return {
        "id": f"e{n}",
        "run_id": "main",
        "ordinal": n,
        "kind": kind,
        "title": title,
        "status": "completed",
        "started_at": f"2026-01-15T10:{start}+00:00",
        "ended_at": f"2026-01-15T10:{end}+00:00",
        **fields,
    }


ROUNDING_TRACE = {
    "schema_version": 1,
    "runs": [
        {
            "id": "main",
            "agent": "claude_code",
            "origin": "recorded",
            "source_format": "claude-code-jsonl",
            "coverage": "partial",
            "warnings": ["Thinking blocks were left out."],
        }
    ],
    "events": [
        event(
            1,
            "assistant",
            "Plan the change",
            "00:05",
            "00:20",
            output="Rounding happens in money.round_amount(). I will make it read the"
            " currency's minor units, then add tests for JPY and BHD.",
        ),
        event(
            2,
            "tool",
            "Read billing/money.py",
            "00:21",
            "00:22",
            parent_id="e1",
            input="billing/money.py",
            output="def round_amount(amount):\n    return amount.quantize(Decimal('0.01'))",
        ),
        event(
            3,
            "tool",
            "Run tests",
            "01:10",
            "01:31",
            parent_id="e1",
            exit_code=1,
            input="pytest -q",
            status="failed",
            output="FAILED test_refund_rounding - round_amount() missing argument 'currency'",
        ),
        event(
            4,
            "assistant",
            "Fix the refund caller",
            "01:32",
            "01:40",
            output="refunds.py still calls round_amount() without a currency.",
        ),
        event(
            5,
            "tool",
            "Run tests",
            "01:45",
            "02:09",
            parent_id="e4",
            exit_code=0,
            input="pytest -q",
            output="212 passed in 23.4s",
        ),
    ],
}

PLAN = """# Keep a ledger of billing exports

## Problem
We cannot tell which rows went into which monthly export,
so finance cannot check a disputed invoice.

## Proposal
1. Add an `export_ledger` table: export id, invoice id, amount, written at.
2. The nightly export job writes one ledger row per exported invoice line.
3. Backfill October from the existing CSV files.

## Out of scope
- Months before October.
"""

CACHE_V1 = body(
    "Price lookups call the pricing service on every request. Cache them in memory for 60 seconds.",
    "- A changed discount can be stale for up to 60 seconds. Is that fine?",
    "`get_price()` is wrapped in a TTL cache keyed by product id.",
    "| Price changes | stale for at most 60 s | `test_ttl_expires` |",
    "| Cache works | `pytest -q` | ✅ 140 passed | — | yes |",
    "- Memory use with all 40k products was not measured.",
    "Revert the commit.",
    "None.",
    "| 1 | Python | 0 | 0 | 0 |",
)

CACHE_V2 = body(
    "Price lookups call the pricing service on every request. Cache them in memory for 60 seconds,"
    " but never cache discounts.",
    "- Base prices can be stale for up to 60 seconds; discounts are always read fresh."
    " Is that the right split?",
    "`get_price()` is wrapped in a TTL cache keyed by product id. `get_discount()` is not cached.",
    "| Price changes | stale for at most 60 s | `test_ttl_expires` |\n"
    "| Discount changes | read fresh every time | `test_discounts_not_cached` |",
    "| Cache works | `pytest -q` | ✅ 141 passed | — | yes |\n"
    "| Memory with 40k products | `python scripts/cache_size.py` | ✅ 18 MB | — | no |",
    "- None left.",
    "Revert the commit.",
    "None.",
    "| 1 | Python | 0 | 0 | 0 |",
)

REPORTS = [
    {
        "id": "demo-prorate-mid-cycle",
        "project": "billing",
        "title": "Prorate mid-cycle plan changes",
        "risk": "high",
        "body": TEMPLATE.read_text(),
        "author": "Alice",
        "commit": "a1b2c3d",
    },
    {
        "id": "demo-currency-rounding",
        "project": "billing",
        "title": "Round amounts per currency",
        "risk": "medium",
        "body": ROUNDING,
        "trace": ROUNDING_TRACE,
        "author": "Alice",
        "commit": "4e5f6a7",
    },
    {
        "id": "demo-export-ledger",
        "project": "data",
        "title": "Keep a ledger of billing exports",
        "risk": "medium",
        "workflow": "plan_first",
        "plan": PLAN,
        "author": "Alice",
    },
    {
        "id": "demo-price-cache",
        "project": "shop",
        "title": "Cache price lookups for 60 seconds",
        "risk": "low",
        "body": CACHE_V1,
        "author": "Alice",
        "commit": "9f20c4d",
    },
    {
        "id": "demo-price-cache",
        "project": "shop",
        "title": "Cache price lookups for 60 seconds",
        "risk": "low",
        "body": CACHE_V2,
        "author": "Alice",
        "commit": "d51a7e2",
        "expected_version": 1,
    },
]


def call(token, path, payload=None):
    request = urllib.request.Request(
        f"{URL}/api{path}",
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def exists(token, id):
    try:
        call(token, f"/reports/{id}")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise
    return True


def main():
    token = os.environ.get("VERIFAIR_API_TOKEN")
    if not token:
        sys.exit("set VERIFAIR_API_TOKEN, for example VERIFAIR_API_TOKEN=change-me")
    if (
        urlparse(URL).hostname not in ("localhost", "127.0.0.1")
        and os.environ.get("VERIFAIR_ALLOW_REMOTE") != "1"
    ):
        sys.exit(f"refusing to load demo data into {URL}; set VERIFAIR_ALLOW_REMOTE=1 to allow it")
    try:
        present = {r["id"] for r in REPORTS if exists(token, r["id"])}
    except urllib.error.HTTPError as error:
        sys.exit(f"HTTP {error.code} from {URL}: {error.read().decode()[:300]}")
    except urllib.error.URLError as error:
        sys.exit(f"cannot reach verifair at {URL} ({error.reason}). Is it running?")
    for report in REPORTS:
        if report["id"] in present:
            print(f"{report['id']} already there, skipped")
            continue
        try:
            result = call(
                token, "/reports", report | {"agent": "example-agent", "model": "example-model-1"}
            )
        except urllib.error.HTTPError as error:
            sys.exit(f"{report['id']}: HTTP {error.code} {error.read().decode()[:300]}")
        print(f"{result['id']} v{result['version']}")
    print(f"Done. Open {URL}")


if __name__ == "__main__":
    main()
