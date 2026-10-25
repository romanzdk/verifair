import json
import os
import subprocess
import sys
from pathlib import Path

EXAMPLE = Path(__file__).resolve().parents[1] / "docs/examples/trace-report.py"


def run_example(*args):
    return subprocess.run(
        [sys.executable, EXAMPLE, *map(str, args)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
    )


def test_example_emits_payload_and_preserves_body_bytes(tmp_path):
    body = tmp_path / "body.md"
    body.write_bytes('# Héllo\r\nquote: \\"x\\"\r\n'.encode())
    result = run_example("--project", "demo", "--title", "A title", "--risk", "low", "--body", body)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "project": "demo",
        "title": "A title",
        "risk": "low",
        "body": '# Héllo\r\nquote: \\"x\\"\r\n',
    }


def test_example_includes_only_explicit_optional_fields(tmp_path):
    body = tmp_path / "body.md"
    plan = tmp_path / "plan.md"
    trace = tmp_path / "trace.json"
    body.write_text("Body", encoding="utf-8")
    plan.write_bytes("Plan: café\r\n".encode())
    trace.write_text(
        '{"schema_version":1,"runs":[{"id":"manual-1","agent":"other",'
        '"origin":"reconstructed","source_format":"manual","coverage":"unknown",'
        '"warnings":[],"basis":"Synthetic example; no actual execution."}],'
        '"events":[{"id":"summary-1","run_id":"manual-1","ordinal":1,'
        '"kind":"summary","title":"Illustration","status":"unknown"}]}',
        encoding="utf-8",
    )
    result = run_example(
        "--project",
        "demo",
        "--title",
        "A title",
        "--risk",
        "medium",
        "--body",
        body,
        "--trace",
        trace,
        "--plan",
        plan,
        "--id",
        "review-1",
        "--expected-version",
        "2",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "project": "demo",
        "title": "A title",
        "risk": "medium",
        "body": "Body",
        "plan": "Plan: café\r\n",
        "trace": {
            "schema_version": 1,
            "runs": [
                {
                    "id": "manual-1",
                    "agent": "other",
                    "origin": "reconstructed",
                    "source_format": "manual",
                    "coverage": "unknown",
                    "warnings": [],
                    "basis": "Synthetic example; no actual execution.",
                }
            ],
            "events": [
                {
                    "id": "summary-1",
                    "run_id": "manual-1",
                    "ordinal": 1,
                    "kind": "summary",
                    "title": "Illustration",
                    "status": "unknown",
                    "truncated": False,
                }
            ],
        },
        "id": "review-1",
        "expected_version": 2,
    }


def test_example_rejects_guard_without_id_and_invalid_trace_without_leaking_input(tmp_path):
    body = tmp_path / "body.md"
    trace = tmp_path / "trace.json"
    body.write_text("Body", encoding="utf-8")
    trace.write_text('{"secret":"do not print me"}', encoding="utf-8")
    args = ("--project", "demo", "--title", "A title", "--risk", "high", "--body", body)

    guard = run_example(*args, "--expected-version", "1")
    invalid_trace = run_example(*args, "--trace", trace)

    assert guard.returncode != 0
    assert guard.stdout == ""
    assert "do not print me" not in guard.stderr
    assert invalid_trace.returncode != 0
    assert invalid_trace.stdout == ""
    assert "do not print me" not in invalid_trace.stderr


def test_example_rejects_oversized_request_without_emitting_partial_json(tmp_path):
    body = tmp_path / "body.md"
    body.write_bytes(b"x" * 1_048_570)
    result = run_example("--project", "demo", "--title", "A title", "--risk", "low", "--body", body)

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr
