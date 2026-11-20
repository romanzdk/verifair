import dataclasses
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from verifair.config import DEFAULT_REQUIRED_SECTIONS

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "demo.py"


def load_demo():
    spec = importlib.util.spec_from_file_location("demo", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_reports_are_accepted_with_the_default_required_sections(settings, make_app):
    strict = dataclasses.replace(
        settings, required_sections=tuple(DEFAULT_REQUIRED_SECTIONS.split(","))
    )
    client = TestClient(make_app(strict))
    for report in load_demo().REPORTS:
        response = client.post("/api/reports", json=report, headers={"Authorization": "Bearer t"})
        assert response.status_code == 201, (report["id"], response.text)
    assert (
        client.get("/api/reports/demo-price-cache", headers={"Authorization": "Bearer t"}).json()[
            "current_version"
        ]
        == 2
    )


def test_demo_refuses_a_remote_server():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        env={"VERIFAIR_API_TOKEN": "x", "VERIFAIR_URL": "https://verifair.example.com"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "refusing to load demo data" in result.stderr


def test_demo_prompts_use_the_export_scripts_intro():
    spec = importlib.util.spec_from_file_location(
        "export_prompts", SCRIPT.parents[1] / "skills" / "verifair-review" / "export-prompts.py"
    )
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    assert f"## Prompts\n\n{script.INTRO}\n" in load_demo().ROUNDING


def test_demo_fills_alices_inbox(settings, make_app):
    demo = load_demo()
    client = TestClient(make_app(settings), headers={"Sec-Fetch-Site": "same-origin"})

    def push(report):
        return client.post(
            "/api/reports", json=report, headers={"Authorization": "Bearer t"}
        ).json()

    def act(person, id, action, form):
        response = client.post(
            f"/r/{id}/{action}",
            data=form,
            headers={"Cookie": f"vf_id={person[0]}; vf_name={person[1]}"},
        )
        assert response.status_code == 200, (id, action, response.text[:300])

    for report in demo.REPORTS:
        demo.load(report, push, act)

    def status(id):
        return client.get(f"/api/reports/{id}", headers={"Authorization": "Bearer t"}).json()[
            "status"
        ]

    assert status("demo-prorate-mid-cycle") == "unsigned"
    assert status("demo-currency-rounding") == "awaiting_review"
    assert status("demo-export-ledger") == "awaiting_implementation"
    assert status("demo-price-cache") == "approved"
    assert status("demo-webhook-retries") == "awaiting_review"
    inbox = client.get("/", headers={"Cookie": f"vf_id={demo.ALICE[0]}; vf_name=Alice"}).text
    for group in (
        "Needs your review",
        "Needs your signature",
        "Needs implementation",
        "Waiting on others",
    ):
        assert group in inbox


def test_demo_reports_have_no_template_hints():
    demo = load_demo()
    hints = [line for line in demo.TEMPLATE.read_text().splitlines() if line.startswith("> ")]
    assert hints
    for report in demo.REPORTS:
        assert not any(hint in report.get("body", "") for hint in hints), report["id"]


def test_template_example_charges_the_same_amount_everywhere():
    template = load_demo().TEMPLATE.read_text()
    amounts = [
        re.search(pattern, template, re.M)
        for pattern in (
            r"^\| After \| ([\d.]+) EUR",
            r"Upgrade works on staging .*invoice line ([\d.]+) EUR",
            r'"amount": "([\d.]+)"',
        )
    ]
    assert all(amounts), "an example amount moved; update the patterns"
    assert len({m.group(1) for m in amounts}) == 1, [m.group(1) for m in amounts]
