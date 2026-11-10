import dataclasses
import importlib.util
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
