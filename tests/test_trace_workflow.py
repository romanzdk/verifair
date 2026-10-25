import json
import os
import subprocess
import sys
from contextlib import closing
from html import escape
from pathlib import Path

import pytest

from verifair import db

ROOT = Path(__file__).resolve().parents[1]
BODY = "## Verification\r\nSynthetic workflow check: café 🦊.\r\n"
PLAN = "## Original integration plan\r\nKeep the exact approved text: café 🦊.\r\n"
METADATA = {"project": "trace-integration", "title": "Trace workflow", "risk": "low"}
PROVIDERS = {
    "claude-stream-json": {
        "version": "2.1.309",
        "fixture": "claude/native.jsonl",
        "agent": "claude_code",
        "tools": [("Tool: Bash", "failed", None)],
        "detail": "pytest -q",
    },
    "codex-exec-jsonl": {
        "version": "0.163.0",
        "fixture": "codex/native.jsonl",
        "agent": "codex",
        "tools": [
            ("Command execution", "failed", 2),
            ("MCP tool: synthetic.lookup", "completed", None),
        ],
        "detail": "pytest -q",
    },
    "opencode-v2-export": {
        "version": "2.1.3",
        "fixture": "opencode/settled.json",
        "agent": "opencode",
        "tools": [("Tool: shell", "completed", 0)],
        "detail": "Visible answer",
    },
    "pi-session-jsonl": {
        "version": "1.4.0",
        "fixture": "pi/session-v3.jsonl",
        "agent": "pi",
        "tools": [
            ("Tool: bash", "failed", None),
            ("Tool: bash", "completed", None),
            ("Tool: subagent", "running", None),
        ],
        "detail": "synthetic-public-text",
    },
}
EXCLUDED = (
    "fixture-secret",
    "fixture-password",
    "fixture-result",
    "fixture-key",
    "ENV-NEVER",
    "IMAGE-NEVER",
    "STRUCTURED-NEVER",
    "ENCRYPTED-NEVER",
    "RAW-NEVER",
    "never-copy",
    "SECRET_",
    "NEVER_",
    "PRIVATE_TITLE",
    "/synthetic/private",
    "synthetic-password",
    "synthetic-token",
    "synthetic-hidden",
    "synthetic-image",
    "synthetic-env",
    "synthetic-metadata",
    "synthetic-parent-path",
    "synthetic-alternate",
    "synthetic-signature",
    "synthetic-error-message",
)


def run_python(*args):
    result = subprocess.run(
        [sys.executable, "-B", *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return result.stdout


@pytest.fixture
def report_files(tmp_path):
    body, plan = tmp_path / "body.md", tmp_path / "plan.md"
    body.write_bytes(BODY.encode("utf-8"))
    plan.write_bytes(PLAN.encode("utf-8"))
    return body, plan


@pytest.fixture
def workflow_users(shared_client, shared_settings, as_user):
    owner = as_user("owner@example.com", "Owner")
    reviewer = as_user("reviewer@example.com", "Reviewer")
    with closing(db.connect(shared_settings.database_url)) as conn:
        token, _ = db.create_token(conn, owner_id="owner@example.com", label="integration")
    return {"Authorization": f"Bearer {token}"}, owner, reviewer


def approve(client, users, id, version, target="implementation", revision=None):
    auth, owner, reviewer = users
    form = {"version": version, "target": target}
    if revision is not None:
        form["revision"] = revision
    signed = client.post(f"/r/{id}/sign", data=form, headers=owner)
    assert signed.status_code == 200, signed.text
    approved = client.post(f"/r/{id}/verdict", data=form | {"verdict": "approve"}, headers=reviewer)
    assert approved.status_code == 200, approved.text
    metadata = client.get(f"/api/reports/{id}", headers=auth).json()
    status = metadata["plan_review"]["status"] if target == "plan" else metadata["status"]
    assert status == "approved"


def example_request(body, *args):
    return json.loads(
        run_python(
            ROOT / "docs/examples/trace-report.py",
            "--project",
            METADATA["project"],
            "--title",
            METADATA["title"],
            "--risk",
            METADATA["risk"],
            "--body",
            body,
            *args,
        )
    )


@pytest.mark.parametrize("source_format", PROVIDERS)
@pytest.mark.parametrize("workflow", ["artifact", "plan_first"])
def test_provider_evidence_survives_example_storage_and_view(
    shared_client, shared_settings, workflow_users, report_files, tmp_path, source_format, workflow
):
    spec = PROVIDERS[source_format]
    auth, _, reviewer = workflow_users
    body, plan = report_files
    id = "native-workflow"
    version = 0
    snapshots = []
    if workflow == "plan_first":
        created = shared_client.post(
            "/api/reports",
            json=METADATA | {"id": id, "workflow": workflow, "plan": PLAN},
            headers=auth,
        )
        assert (created.status_code, created.json()) == (
            201,
            {"id": id, "version": 1, "url": f"http://localhost:8000/r/{id}"},
        )
        version = 1
        first = shared_client.get(f"/api/reports/{id}/versions/1", headers=auth).json()
        assert first["body"] is first["trace"] is first["commit"] is None
        assert (first["plan"], first["plan_revision"]) == (PLAN, 1)
        approve(shared_client, workflow_users, id, version, target="plan", revision=1)

    for include_details in (False, True):
        trace_path = tmp_path / f"trace-{include_details}.json"
        options = ["--leaf", "a3"] if source_format == "pi-session-jsonl" else []
        if include_details:
            options.append("--include-details")
        assert (
            run_python(
                "-m",
                "verifair.trace_import",
                "--format",
                source_format,
                "--source-version",
                spec["version"],
                "--input",
                ROOT / "tests/fixtures/traces" / spec["fixture"],
                "--output",
                trace_path,
                *options,
            )
            == ""
        )
        raw = trace_path.read_text(encoding="utf-8")
        document = json.loads(raw)
        assert document["schema_version"] == 1
        assert len(document["runs"]) == 1
        run = document["runs"][0]
        assert (run["agent"], run["origin"], run["source_format"], run["source_version"]) == (
            spec["agent"],
            "recorded",
            source_format,
            spec["version"],
        )
        assert run["coverage"] in {"partial", "unknown"}
        assert [
            (e["title"], e["status"], e.get("exit_code"))
            for e in document["events"]
            if e["kind"] == "tool"
        ] == spec["tools"]
        assert all(e.get("source_ref") for e in document["events"] if e["kind"] == "tool")
        assert all(marker not in raw for marker in EXCLUDED)
        if include_details:
            assert spec["detail"] in raw and "[REDACTED]" in raw
        else:
            assert all(
                not {"input", "output", "explanation"}.intersection(event)
                for event in document["events"]
            )
            assert spec["detail"] not in raw

        args = ["--trace", trace_path]
        if include_details:
            args.extend(["--plan", plan])
        if version:
            args.extend(["--id", id, "--expected-version", str(version)])
        payload = example_request(body, *args)
        assert payload["trace"] == document
        assert payload["body"] == BODY
        if include_details:
            assert payload["plan"] == PLAN
            payload["commit"] = "abc1234"
        response = shared_client.post("/api/reports", json=payload | {"id": id}, headers=auth)
        version += 1
        assert (response.status_code, response.json()) == (
            201,
            {"id": id, "version": version, "url": f"http://localhost:8000/r/{id}"},
        )
        plan_revision = 1 if workflow == "plan_first" else version if include_details else None
        binding = {"version": version}
        if plan_revision is not None:
            binding["plan_revision"] = plan_revision
        if include_details:
            binding["commit"] = "abc1234"
        envelope = {"binding": binding, "document": document}
        expected_plan = PLAN if plan_revision is not None else None
        snapshot = shared_client.get(f"/api/reports/{id}/versions/{version}", headers=auth)
        assert snapshot.status_code == 200
        with closing(db.connect(shared_settings.database_url)) as conn:
            stored = db.get_version(conn, id, version)
            assert stored["trace"] == json.dumps(
                envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            assert snapshot.json() == {
                "version": version,
                "created_at": stored["created_at"],
                "body": BODY,
                "plan": expected_plan,
                "plan_revision": plan_revision,
                "commit": "abc1234" if include_details else None,
                "trace": envelope,
            }
        snapshots.append(snapshot.json())
        metadata = shared_client.get(f"/api/reports/{id}", headers=auth).json()
        assert metadata["status"] == "unsigned"
        assert metadata["versions"][-1]["has_trace"] is True
        assert "trace" not in metadata
        assert all("trace" not in item for item in metadata["versions"])
        page = shared_client.get(f"/r/{id}/v/{version}?tab=execution", headers=reviewer)
        assert page.status_code == 200
        assert "Imported recording (uploader-supplied)" in page.text
        assert f"Capture source: {source_format} · Source version: {spec['version']}" in page.text
        assert f"Attached to version {version}" in page.text
        for event in document["events"]:
            assert f'id="vf_trace_{event["id"]}"' in page.text
            assert escape(event["title"]) in page.text
            assert f'<span class="execution-status">{event["status"]}</span>' in page.text
            if event.get("source_ref"):
                assert escape(event["source_ref"]) in page.text
        assert all(marker not in page.text for marker in EXCLUDED)
        if include_details:
            assert spec["detail"] in page.text and "[REDACTED]" in page.text
            assert "abc1234" in page.text
        else:
            assert spec["detail"] not in page.text
        if expected_plan:
            assert f"Original plan revision {plan_revision}" in page.text
            assert "Keep the exact approved text: café 🦊." in page.text
        else:
            assert "No plan was bound to this implementation." in page.text
        if not include_details:
            approve(shared_client, workflow_users, id, version)

    if workflow == "plan_first":
        edited = shared_client.post(
            "/api/reports",
            json=METADATA
            | {
                "id": id,
                "expected_version": version,
                "plan": "## Replacement plan\nA later plan only.",
            },
            headers=auth,
        )
        assert (edited.status_code, edited.json()["version"]) == (201, version + 1)
        inherited = shared_client.get(f"/api/reports/{id}/versions/{version + 1}", headers=auth)
        assert inherited.json()["trace"] == snapshots[-1]["trace"]
        assert (inherited.json()["body"], inherited.json()["commit"]) == (BODY, "abc1234")
        assert inherited.json()["plan_revision"] == version + 1
        page = shared_client.get(f"/r/{id}?tab=execution", headers=reviewer)
        assert page.status_code == 200
        bound_plan = page.text.split('<details class="execution-plan" open>', 1)[1].split(
            "</details>", 1
        )[0]
        assert "Keep the exact approved text: café 🦊." in bound_plan
        assert "A later plan only." not in bound_plan
        assert "Execution was attached against an older plan." in bound_plan
        assert f"Current plan revision {version + 1}" in bound_plan
    for snapshot in snapshots:
        historical = shared_client.get(
            f"/api/reports/{id}/versions/{snapshot['version']}", headers=auth
        )
        assert historical.json() == snapshot


@pytest.mark.parametrize("workflow", ["artifact", "plan_first"])
def test_example_without_trace_keeps_ordinary_workflows_optional(
    shared_client, workflow_users, report_files, workflow
):
    auth, _, reviewer = workflow_users
    body, plan = report_files
    id = "ordinary-workflow"
    version = 0
    if workflow == "plan_first":
        created = shared_client.post(
            "/api/reports",
            json=METADATA | {"id": id, "workflow": workflow, "plan": PLAN},
            headers=auth,
        )
        assert created.status_code == 201
        version = 1
        approve(shared_client, workflow_users, id, version, target="plan", revision=1)
    args = ["--plan", plan]
    if version:
        args.extend(["--id", id, "--expected-version", str(version)])
    payload = example_request(body, *args)
    assert "trace" not in payload
    response = shared_client.post("/api/reports", json=payload | {"id": id}, headers=auth)
    version += 1
    assert (response.status_code, response.json()) == (
        201,
        {"id": id, "version": version, "url": f"http://localhost:8000/r/{id}"},
    )
    snapshot = shared_client.get(f"/api/reports/{id}/versions/{version}", headers=auth).json()
    assert (snapshot["body"], snapshot["plan"], snapshot["trace"]) == (BODY, PLAN, None)
    metadata = shared_client.get(f"/api/reports/{id}", headers=auth).json()
    assert metadata["status"] == "unsigned"
    assert all(item["has_trace"] is False for item in metadata["versions"])
    page = shared_client.get(f"/r/{id}/v/{version}?tab=execution", headers=reviewer)
    assert page.status_code == 200
    assert "No execution trace attached to this version." in page.text
    approve(shared_client, workflow_users, id, version)
