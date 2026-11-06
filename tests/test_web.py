import dataclasses
import importlib.metadata
import re
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote as url_quote

import pytest
from conftest import OIDC, SessionClient, signed_in
from fastapi.testclient import TestClient

from verifair import db, notify, web
from verifair.app import CSP as APP_CSP
from verifair.traces import validate_document

H = {"Authorization": "Bearer t"}
REFUSED_COPY = "Add a summary, or start a comment thread of your own and leave it open."
SAMPLE = "## Intent\nFull new price.\n\n## Edge cases\nChange on the first day."


def push(client, **overrides):
    payload = {
        "project": "billing-service",
        "title": "Prorate",
        "risk": "high",
        "ticket_url": "https://t/BILL-412",
        "body": SAMPLE,
    }
    return client.post("/api/reports", json=payload | overrides, headers=H)


def sign(client, id="prorate", version=1):
    client.cookies.set("vf_id", "signer00")
    client.cookies.set("vf_name", "Signer")
    r = client.post(f"/r/{id}/sign", data={"version": version})
    client.cookies.set("vf_id", "review00")
    client.cookies.delete("vf_name")
    return r


def api_report(client, id="prorate"):
    return client.get(f"/api/reports/{id}", headers=H).json()


def api_threads(client, id="prorate"):
    return client.get(f"/api/reports/{id}/comments", headers=H).json()["threads"]


def _set_plan(settings, plan, version=1):
    with closing(db.connect(settings.database_url)) as conn:
        conn.execute(
            "UPDATE versions SET plan = %s, plan_revision = %s "
            "WHERE report_id = 'prorate' AND version = %s",
            (plan, version if plan is not None else None, version),
        )


def _document_nav(page):
    nav = re.search(r'<nav class="tabs" aria-label="Report documents">(.*?)</nav>', page, re.S)
    assert nav is not None
    assert nav[1].count('aria-current="page"') == 1
    return nav[1]


def _viewer_trace(*, runs=None, events=None):
    return validate_document(
        {
            "schema_version": 1,
            "runs": runs
            or [
                {
                    "id": "recording",
                    "agent": "codex",
                    "origin": "recorded",
                    "source_format": "codex-jsonl",
                    "source_version": "0.9",
                    "coverage": "partial",
                    "warnings": ["Only this session was captured."],
                }
            ],
            "events": events
            or [
                {
                    "id": "tool-1",
                    "run_id": "recording",
                    "ordinal": 1,
                    "kind": "tool",
                    "title": "Run checks",
                    "status": "completed",
                    "model": "trace-model",
                    "output": "Tool finished; inspect the evidence.",
                }
            ],
        }
    )


def _seed_execution(settings, *, workflow="artifact", trace=None):
    with closing(db.connect(settings.database_url)) as conn:
        if workflow == "plan_first":
            db.push_report(
                conn,
                id="execution",
                project="p",
                title="Execution",
                risk="low",
                body=None,
                body_supplied=False,
                workflow=workflow,
                plan="## Bound plan\nOriginal approved text.",
            )
            db.sign_version(
                conn,
                report_id="execution",
                version=1,
                target="plan",
                expected_version=1,
                signer_id="owner000",
                signer_name="Owner",
            )
            db.add_verdict(
                conn,
                report_id="execution",
                version=1,
                target="plan",
                reviewer_id="judge000",
                reviewer_name="Judge",
                verdict="approve",
            )
        _, version = db.push_report(
            conn,
            id="execution",
            project="p",
            title="Execution",
            risk="low",
            body=SAMPLE,
            expected_version=1 if workflow == "plan_first" else None,
            trace=trace or _viewer_trace(),
            commit="bound-commit",
            agent="LATEST_AGENT",
            model="LATEST_MODEL",
            session_url="https://latest.example/session",
            reviewers=["carol000"],
        )
        assert db.get_trace(conn, "execution", version) is not None
    return version


@pytest.mark.parametrize("workflow", ["artifact", "plan_first"])
def test_execution_tab_is_read_only_in_both_workflows(client, settings, workflow):
    version = _seed_execution(settings, workflow=workflow)
    for name in (None, "Reader"):
        if name:
            client.cookies.set("vf_name", name)
        response = client.get("/r/execution?tab=execution")
        assert response.status_code == 200
        nav = _document_nav(response.text)
        assert '<a href="/r/execution?tab=execution" aria-current="page">Execution</a>' in nav
        assert (">Plan</a>" in nav) == (workflow == "plan_first")
        assert (">Implementation</a>" in nav) == (workflow == "plan_first")
        assert (">Report</a>" in nav) == (workflow == "artifact")
        assert response.context["target"] == "implementation"
        assert response.context["version"] == version
        for forbidden in (
            "hx-post=",
            "data-section=",
            "data-comment-section",
            'class="cform"',
            'id="vf_popover"',
            'id="vf_verdict"',
            'id="vf_sign"',
            'class="name-prompt"',
            "has-verdict",
            "Remove review request",
            'class="addrev"',
        ):
            assert forbidden not in response.text
        document_url = (
            "/r/execution?tab=implementation" if workflow == "plan_first" else "/r/execution"
        )
        assert f'href="{document_url}">Review ' in response.text
    document = client.get(document_url)
    assert 'id="vf_sign"' in document.text and 'id="vf_verdict"' in document.text
    assert f'name="version" value="{version}"' in document.text
    assert f'name="revision" value="{version}"' in document.text
    assert 'name="target" value="implementation"' in document.text
    assert '<a href="/r/execution?tab=execution">Execution</a>' in _document_nav(document.text)


def test_execution_shared_reviewer_controls_are_hidden(shared_client, shared_settings, as_user):
    as_user("other@example.com", "Other")
    _seed_execution(shared_settings)
    assert 'class="addrev"' in shared_client.get("/r/execution").text
    page = shared_client.get("/r/execution?tab=execution").text
    assert "hx-post=" not in page and 'class="addrev"' not in page


@pytest.mark.parametrize("workflow", ["artifact", "plan_first"])
def test_execution_history_keeps_selection_for_untraced_snapshots(client, settings, workflow):
    traced_version = _seed_execution(settings, workflow=workflow)
    with closing(db.connect(settings.database_url)) as conn:
        db.push_report(
            conn,
            id="execution",
            project="p",
            title="Execution",
            risk="low",
            body="## Intent\nNew document without evidence.",
            expected_version=traced_version,
            agent="HISTORY_LATEST_AGENT",
            model="HISTORY_LATEST_MODEL",
            session_url="https://history-latest.example/session",
        )
    latest = traced_version + 1
    history = client.get(f"/r/execution/v/{traced_version}?tab=execution")
    assert "Run checks" in history.text
    assert "HISTORY_LATEST" not in history.text and "https://history-latest" not in history.text
    assert 'href="/r/execution?tab=execution">open current</a>' in history.text
    for version in range(1, latest + 1):
        response = client.get(f"/r/execution/v/{version}?tab=execution")
        nav = _document_nav(response.text)
        prefix = "/r/execution" if version == latest else f"/r/execution/v/{version}"
        assert f'href="{prefix}?tab=execution" aria-current="page">Execution</a>' in nav
        rail = response.text.split('<aside class="side"')[1].split("</aside>")[0]
        for n in range(1, latest + 1):
            href = "/r/execution" if n == latest else f"/r/execution/v/{n}"
            assert f'href="{href}?tab=execution"' in rail
        assert ("No execution trace attached to this version" in response.text) == (
            version != traced_version
        )
    assert ">Execution</a>" not in client.get("/r/execution").text
    assert (
        "No execution trace attached to this version"
        in client.get("/r/execution?tab=execution").text
    )
    for path in ("/r/missing?tab=execution", "/r/execution/v/99?tab=execution"):
        assert client.get(path).status_code == 404


def test_execution_explicit_empty_url_does_not_fall_back_to_report(client):
    push(client)
    response = client.get("/r/prorate?tab=execution")
    assert response.status_code == 200
    assert "No execution trace attached to this version" in response.text
    assert "Full new price." not in response.text
    assert '?tab=execution" aria-current="page">Execution</a>' in _document_nav(response.text)
    assert 'href="/r/prorate">Review Report</a>' in response.text


def test_execution_uses_bound_plan_after_later_plan_update(client, settings):
    _seed_execution(settings, workflow="plan_first")
    with closing(db.connect(settings.database_url)) as conn:
        before = db.get_trace(conn, "execution", 2)
        db.push_report(
            conn,
            id="execution",
            project="p",
            title="Execution",
            risk="low",
            body=None,
            body_supplied=False,
            plan="## New plan\nLater replacement text.",
            expected_version=2,
        )
        assert db.get_trace(conn, "execution", 3) == before
    for path in ("/r/execution?tab=execution", "/r/execution/v/2?tab=execution"):
        response = client.get(path)
        assert "Original approved text." in response.text
        assert "Later replacement text." not in response.text
        assert "Plan used for this implementation" in response.text
        assert "Original plan revision 1" in response.text
        assert "Attached to version 2" in response.text and "bound-commit" in response.text
        assert 'href="/r/execution?tab=plan">Current plan revision 3</a>' in response.text
        assert "older plan" in response.text
        assert "all steps covered" not in response.text.lower()
    plan = client.get("/r/execution?tab=plan").text
    assert 'name="target" value="plan"' in plan
    assert 'name="revision" value="3"' in plan


def test_execution_provenance_and_unknown_telemetry_are_explicit(client, settings):
    runs = [
        {
            "id": "recording",
            "agent": "codex",
            "origin": "recorded",
            "source_format": "jsonl",
            "source_version": "1.2",
            "coverage": "partial",
            "warnings": ["Missing earlier turns."],
        },
        {
            "id": "manual",
            "agent": "other",
            "origin": "reconstructed",
            "source_format": "manual",
            "coverage": "unknown",
            "warnings": [],
            "basis": "Rebuilt from a human note.",
        },
    ]
    events = [
        {
            "id": "done",
            "run_id": "recording",
            "ordinal": 1,
            "kind": "tool",
            "title": "Check command",
            "status": "completed",
            "model": "trace-model",
            "started_at": "2026-10-25T10:00:00Z",
            "ended_at": "2026-10-25T10:00:02Z",
            "exit_code": 0,
        },
        {
            "id": "failed",
            "run_id": "recording",
            "ordinal": 2,
            "kind": "tool",
            "title": "Failed command",
            "status": "failed",
            "exit_code": 2,
            "truncated": True,
        },
        {
            "id": "running",
            "run_id": "recording",
            "ordinal": 3,
            "kind": "tool",
            "title": "Active command",
            "status": "running",
            "started_at": "2026-10-25T10:00:03Z",
        },
        {
            "id": "note",
            "run_id": "manual",
            "ordinal": 1,
            "kind": "summary",
            "title": "Human note",
            "status": "unknown",
        },
    ]
    _seed_execution(settings, trace=_viewer_trace(runs=runs, events=events))
    page = client.get("/r/execution?tab=execution").text
    for expected in (
        "Imported recording (uploader-supplied)",
        "Agent-written reconstruction",
        "Missing earlier turns.",
        "Coverage: partial",
        "Coverage: unknown",
        "Rebuilt from a human note.",
        "trace-model",
        "jsonl",
        "1.2",
        "Not recorded",
        "Duration: 2 s",
        "failed",
        "running",
        "Truncated",
        "Exit code",
        ">2</",
        "not necessarily individual model API requests",
        "No plan was bound",
    ):
        assert expected in page
    for misleading in (
        "LATEST_AGENT",
        "LATEST_MODEL",
        "https://latest.example/session",
        "tests passed",
    ):
        assert misleading not in page
    assert "does not prove" in page and "server authentication" in page
    assert page.index("Capture source: jsonl") < page.index('<div class="execution-layout')
    assert page.index("Missing earlier turns.") < page.index('<div class="execution-layout')
    for event_id in ("failed", "running", "note"):
        event_html = page.split(f'id="vf_trace_{event_id}"')[1].split("</details>")[0]
        assert "Duration: Not recorded" in event_html


def test_execution_tree_respects_explicit_parentage_and_event_order(client, settings):
    base_run = _viewer_trace().runs[0].model_dump()
    runs = [
        base_run,
        base_run | {"id": "nested", "parent_run_id": "recording", "parent_event_id": "parent"},
        base_run | {"id": "run-child", "parent_run_id": "nested"},
        base_run | {"id": "unlinked"},
    ]
    events = [
        {
            "id": "later",
            "run_id": "recording",
            "ordinal": 9,
            "parent_id": "parent",
            "kind": "tool",
            "title": "Later child",
            "status": "unknown",
        },
        {
            "id": "parent",
            "run_id": "recording",
            "ordinal": 1,
            "kind": "assistant",
            "title": "Parent step",
            "status": "unknown",
        },
        {
            "id": "earlier",
            "run_id": "recording",
            "ordinal": 3,
            "parent_id": "parent",
            "kind": "tool",
            "title": "Earlier child",
            "status": "unknown",
        },
        *[
            {
                "id": f"event-{run}",
                "run_id": run,
                "ordinal": 1,
                "kind": "assistant",
                "title": run,
                "status": "unknown",
            }
            for run in ("nested", "run-child", "unlinked")
        ],
    ]
    _seed_execution(settings, trace=_viewer_trace(runs=runs, events=events))
    response = client.get("/r/execution?tab=execution")
    tree = response.context["execution_tree"]
    assert [node["run"].id for node in tree] == ["recording", "unlinked"]
    parent = tree[0]["children"][0]
    assert parent["event"].id == "parent"
    assert [node["event"].id for node in parent["children"][:2]] == ["earlier", "later"]
    assert parent["children"][2]["run"].id == "nested"
    assert parent["children"][2]["children"][-1]["run"].id == "run-child"
    assert response.text.index('id="vf_trace_earlier"') < response.text.index('id="vf_trace_later"')
    assert '<details class="execution-node" id="vf_trace_parent">' in response.text
    assert 'href="#vf_trace_parent"' in response.text
    assert "separate roots" in response.text and "cross-run causality" in response.text


def test_execution_fields_are_plain_text_and_bound_plan_uses_safe_renderer(client, settings):
    hostile = "</summary><script>alert(1)</script> javascript:alert(2) /private/session.jsonl"
    run = _viewer_trace().runs[0].model_dump() | {
        "source_format": "javascript:evil",
        "warnings": [hostile],
    }
    event = _viewer_trace().events[0].model_dump() | {
        "title": hostile,
        "input": hostile,
        "output": '<img src="https://evil.example/pixel" onerror="evil()">',
        "explanation": hostile,
        "source_ref": "javascript:evil /private/session.jsonl",
        "model": hostile,
    }
    _seed_execution(
        settings, workflow="plan_first", trace=_viewer_trace(runs=[run], events=[event])
    )
    with closing(db.connect(settings.database_url)) as conn:
        conn.execute(
            "UPDATE versions SET plan = %s WHERE report_id = 'execution' AND version = 1",
            ("## Original\n**Safe bold**\n\n<script>evil()</script>\n\n[bad](javascript:evil)",),
        )
    page = client.get("/r/execution?tab=execution").text
    assert "&lt;/summary&gt;&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;img src=&#34;https://evil.example/pixel&#34;" in page
    assert "<strong>Safe bold</strong>" in page and "&lt;script&gt;evil()&lt;/script&gt;" in page
    for forbidden in (
        "<script>alert",
        "<script>evil",
        '<img src="https://evil.example',
        'href="javascript:',
        'href="/private',
        'src="/private',
    ):
        assert forbidden not in page


def test_plan_template_is_read_only(client):
    push(client, reviewers=["carol000"])
    client.cookies.set("vf_name", "Reader")
    response = client.get("/r/prorate")
    context = dict(response.context)
    context |= {
        "view_tab": "plan",
        "plan_html": "<h1>Original plan</h1>",
        "shown_url": "/r/prorate",
    }
    page = web.templates.get_template("report.html").render(context)
    assert '<article class="content">' in page
    assert "<h1>Original plan</h1>" in page
    assert "has-verdict" not in page and "hx-post=" not in page
    assert "data-section=" not in page and 'id="vf_popover"' not in page
    assert "Full new price." not in page


def test_report_plan_documents_navigation(client, settings):
    push(client)
    assert "Report documents" not in client.get("/r/prorate").text
    _set_plan(settings, "# Original plan\n\n## Design\nPlan-only rationale.")
    comment = client.post(
        "/api/reports/prorate/comments", json={"body": "Report-only comment"}, headers=H
    )
    assert comment.status_code == 201
    for suffix in ("", "?tab=report", "?tab=unknown", "?tab="):
        response = client.get("/r/prorate" + suffix)
        assert response.status_code == 200
        nav = _document_nav(response.text)
        assert '<a href="/r/prorate" aria-current="page">Report</a>' in nav
        assert '<a href="/r/prorate?tab=plan">Plan</a>' in nav
        assert "Full new price." in response.text and "Report-only comment" in response.text
        assert "Plan-only rationale." not in response.text
    plan = client.get("/r/prorate?tab=plan")
    assert plan.status_code == 200
    nav = _document_nav(plan.text)
    assert '<a href="/r/prorate?tab=plan" aria-current="page">Plan</a>' in nav
    assert "<h1>Original plan</h1>" in plan.text and "<h2>Design</h2>" in plan.text
    assert "Report-only comment" not in plan.text and "Full new price." not in plan.text
    assert plan.text.count("<main") == 1
    assert plan.context["is_current"] is True
    assert plan.context["shown_url"] == "/r/prorate"
    for path in ("/r/nope?tab=plan", "/r/prorate/v/99?tab=plan"):
        assert client.get(path).status_code == 404
    _set_plan(settings, None)
    fallback = client.get("/r/prorate?tab=plan")
    assert fallback.status_code == 200
    assert "Report documents" not in fallback.text and "Full new price." in fallback.text


def test_plan_page_is_read_only(client, settings):
    push(client, reviewers=["carol000"])
    _set_plan(settings, "# Plan\n\n## Intent\nRead-only design.")
    client.cookies.set("vf_name", "Reader")
    report = client.get("/r/prorate").text
    assert 'id="vf_sign"' in report and 'id="vf_verdict"' in report
    assert "data-comment-section" in report and "has-verdict" in report
    assert "Remove review request for carol000" in report
    plan = client.get("/r/prorate?tab=plan").text
    for forbidden in (
        "hx-post=",
        "data-section=",
        "data-comment-section",
        'class="cform"',
        'id="vf_popover"',
        'id="vf_verdict"',
        'id="vf_sign"',
        'class="name-prompt"',
        "has-verdict",
        "Remove review request",
        'class="addrev"',
    ):
        assert forbidden not in plan
    assert "carol000" in plan and "Read-only context" in plan
    assert 'on the <a href="/r/prorate">Report tab</a>' in plan
    client.cookies.delete("vf_name")
    assert 'class="name-prompt"' not in client.get("/r/prorate?tab=plan").text
    sign(client)
    assert "Signed by <b>Signer</b>" in client.get("/r/prorate?tab=plan").text
    push(client, id="prorate")
    historical = client.get("/r/prorate/v/1").text
    assert "has-verdict" not in historical and "data-comment-section" not in historical


def test_plan_shared_reviewers_are_read_only(shared_client, shared_settings, as_user):
    as_user("reviewer@example.com", "Reviewer")
    as_user("other@example.com", "Other")
    with closing(db.connect(shared_settings.database_url)) as conn:
        db.push_report(
            conn,
            id="prorate",
            project="p",
            title="Prorate",
            risk="low",
            body=SAMPLE,
            author_id="admin@example.com",
            reviewers=["reviewer@example.com"],
        )
    _set_plan(shared_settings, "# Shared plan")
    report = shared_client.get("/r/prorate").text
    assert 'class="addrev"' in report and "Remove review request" in report
    plan = shared_client.get("/r/prorate?tab=plan").text
    assert "Reviewer" in plan and "hx-post=" not in plan
    assert 'class="addrev"' not in plan and "Remove review request" not in plan


def test_plan_history_and_version_links(client, settings):
    push(client)
    _set_plan(settings, "# Plan A\nOriginal choice.")
    client.post("/api/reports/prorate/comments", json={"body": "General note"}, headers=H)
    client.post(
        "/api/reports/prorate/comments",
        json={"body": "Gone note", "section": "Edge cases"},
        headers=H,
    )
    push(client, id="prorate", body="## Intent\nChanged body.")
    _set_plan(settings, "# Plan B\nReplacement choice.", 2)
    first = client.get("/r/prorate/v/1?tab=plan")
    assert first.context["is_current"] is False
    assert first.context["shown_url"] == "/r/prorate/v/1"
    assert "Original choice." in first.text and "Replacement choice." not in first.text
    assert 'href="/r/prorate?tab=plan">open current</a>' in first.text
    push(client, id="prorate", body="## Intent\nNewest body.")
    _set_plan(settings, None, 3)
    for version, wanted, unwanted in (
        (1, "Original choice.", "Replacement choice."),
        (2, "Replacement choice.", "Original choice."),
    ):
        response = client.get(f"/r/prorate/v/{version}?tab=plan")
        page = response.text
        assert wanted in page and unwanted not in page
        assert f'href="/r/prorate/v/{version}" aria-current="page">' not in _document_nav(page)
        assert f'href="/r/prorate/v/{version}?tab=plan" aria-current="page">Plan</a>' in page
        assert 'href="/r/prorate">open current</a>' in page
        rail = page.split('<aside class="side"')[1].split("</aside>")[0]
        assert 'href="/r/prorate/v/1?tab=plan"' in rail
        assert 'href="/r/prorate/v/2?tab=plan"' in rail
        assert 'href="/r/prorate"' in rail and 'href="/r/prorate?tab=plan"' not in rail
        assert f'href="/r/prorate/v/{version}#intent"' in rail
        assert f'href="/r/prorate/v/{version}#vf_general"' in rail
        if version == 2:
            assert f'href="/r/prorate/v/{version}#vf_earlier"' in rail
        assert 'href="#intent"' not in rail
    for path in ("/r/prorate?tab=plan", "/r/prorate/v/3?tab=plan"):
        fallback = client.get(path)
        assert "Newest body." in fallback.text and "Report documents" not in fallback.text
    report = client.get("/r/prorate/v/1").text
    assert 'href="/r/prorate/v/2?tab=plan"' not in report


def test_plan_headings_do_not_become_report_comment_anchors(client, settings):
    push(client, body="## Intent\nFirst body quote.\n\n## Intent\nSecond body quote.")
    _set_plan(
        settings,
        "# Plan\n\n## Intent\nPlan-only quote.\n\n## Design\nPrivate design.\n\n"
        "<script>alert(1)</script>\n\n{{ 7 * 7 }}\n\n[bad](javascript:alert(1))",
    )
    page = client.get("/r/prorate?tab=plan").text
    assert '<article class="content">' in page and "<h2>Intent</h2>" in page
    assert 'id="intent"' not in page and "data-section=" not in page
    assert "&lt;script&gt;" in page and "<script>alert(1)" not in page
    assert "{{ 7 * 7 }}" in page and 'href="javascript:' not in page
    unknown = client.post(
        "/api/reports/prorate/comments",
        json={"body": "Why?", "section": "Design"},
        headers=H,
    )
    assert unknown.status_code == 422 and unknown.json()["detail"] == "unknown section"
    quote = client.post(
        "/api/reports/prorate/comments",
        json={"body": "Why?", "section": "Intent", "quote": "Plan-only quote."},
        headers=H,
    )
    assert quote.status_code == 422 and quote.json()["detail"] == "quote not found"
    valid = client.post(
        "/api/reports/prorate/comments",
        json={"body": "Real comment", "section": "Intent", "quote": "Second body quote."},
        headers=H,
    )
    assert valid.status_code == 201
    with closing(db.connect(settings.database_url)) as conn:
        notice = notify.comment_text(
            db.get_report(conn, "prorate"), db.list_comments(conn, "prorate")[0], settings.base_url
        )
    link = notice.rsplit(" ", 1)[1]
    assert link == f"{settings.base_url}/r/prorate#vf_c{valid.json()['id']}"
    report = client.get(link.removeprefix(settings.base_url)).text
    assert 'id="intent"' in report and 'id="intent-2"' in report
    assert f'id="vf_c{valid.json()["id"]}"' in report
    assert "Real comment" in report and "Plan-only quote." not in report


def test_inbox_links_projects_and_changes_shows_counts(client):
    push(client)
    r = client.get("/")
    assert r.status_code == 200 and "<h1>Inbox</h1>" in r.text
    assert 'href="/p/billing-service"' in r.text
    changes = client.get("/changes").text
    assert "billing-service" in changes and 'aria-label="Unsigned · 1"' in changes


def test_age_and_ago():
    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    assert web.age("2026-10-24T11:59:30+00:00", now) == "<1m"
    assert web.age("2026-10-24T11:35:00+00:00", now) == "25m"
    assert web.age("2026-10-24T09:00:00+00:00", now) == "3h"
    assert web.age("2026-10-22T12:00:00+00:00", now) == "2d"
    assert web.ago("2026-10-24T11:59:30+00:00", now) == "just now"
    assert web.ago("2026-10-22T12:00:00+00:00", now) == "2d ago"
    assert web.ago("2026-09-01T00:00:00+00:00", now) == "1 Sep 2026"
    assert re.fullmatch(r"c[0-4]", web.avatar("alice000"))
    assert web.avatar("alice000") == web.avatar("alice000")


def test_changes_tabs_default_to_open_and_count_by_status(client):
    push(client)
    push(client, title="Retry webhooks", risk="low", ticket_url=None)
    push(client, title="Unsigned one", ticket_url=None)
    sign(client)
    sign(client, "retry-webhooks")
    client.cookies.set("vf_name", "Alice")
    client.post("/r/retry-webhooks/verdict", data={"version": 1, "verdict": "approve"})

    def page_of(url):
        r = client.get(url)
        assert r.status_code == 200
        return r.text

    everything = page_of("/changes")
    assert 'aria-label="Open · 2"' in everything
    assert 'aria-label="Approved · 1"' in everything
    assert 'aria-label="All · 3"' in everything
    assert re.search(r'<a href="\?tab=open"[^>]*aria-current="page"', everything)
    assert "Prorate" in everything and "Unsigned one" in everything
    assert "Retry webhooks" not in everything
    assert "billing-service ·" in everything

    approved = page_of("/changes?tab=approved")
    assert "Retry webhooks" in approved
    assert "Prorate" not in approved and "Unsigned one" not in approved
    bogus = page_of("/changes?tab=bogus")
    assert bogus.split('<main class="app-page"')[1] == everything.split('<main class="app-page"')[1]
    assert "Retry webhooks" in page_of("/changes?tab=all")
    pending = page_of("/changes?tab=awaiting_review")
    assert "Prorate" in pending and "Unsigned one" not in pending

    project = page_of("/p/billing-service")
    for label in ('aria-label="Open · 2"', 'aria-label="Approved · 1"', 'aria-label="All · 3"'):
        assert label in project
    rows = project.split('<main class="app-page"')[1]
    assert "billing-service ·" not in rows
    assert "BILL-412" in rows and "Retry webhooks" not in rows
    assert page_of("/p/billing-service?tab=bogus").count('class="chg"') == 2
    assert client.get("/p/nope").status_code == 404


def test_change_row_shows_risk_reviewers_and_threads(client):
    push(client, reviewers=["bob00000"], risk="high")
    sign(client)
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve"})
    client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"})
    page = client.get("/changes?tab=all").text
    row = page.split('<a class="chg"')[1].split("</a>")[0]
    assert 'class="risk high"' in row and "High" in row
    assert 'class="badge b-approved"' in row
    assert 'class="v ok"' in row
    assert 'title="Alice · approved v1"' in row
    assert 'title="bob00000 · review requested"' in row
    assert 'title="1 open thread"' in row
    assert 'title="Author: agent"' in row and "icons.svg#bot" in row
    assert "💬" not in page


def test_unsigned_project_opens_with_its_changes_visible(client):
    push(client, title="Draft evidence report")
    page = client.get("/p/billing-service")
    assert "Draft evidence report" in page.text
    assert 'aria-current="page"' in page.text
    filtered = client.get("/p/billing-service?tab=awaiting_review")
    assert "Nothing awaiting review" in filtered.text
    assert 'href="?tab=all"' in filtered.text


def test_changes_tabs_link_to_real_statuses_and_inbox_shows_own_signed_work(client):
    push(client, title="Ready evidence")
    sign(client, "ready-evidence")
    push(client, title="Later unsigned report")
    page = client.get("/changes").text
    assert 'href="?tab=awaiting_review"' in page and 'href="?tab=unsigned"' in page
    ready = client.get("/changes?tab=awaiting_review").text
    assert 'href="/r/ready-evidence"' in ready
    assert 'href="/r/later-unsigned-report"' not in ready
    client.cookies.set("vf_id", "signer00")
    waiting = client.get("/").text.split('<details class="grp"')[1]
    assert 'href="/r/ready-evidence"' in waiting
    assert 'href="/r/later-unsigned-report"' not in waiting


def test_first_run_and_caught_up(client, shared_client, settings):
    page = client.get("/").text
    assert "Welcome to verifair" in page
    assert "export VERIFAIR_URL=http://localhost:8000" in page
    assert 'href="/access/tokens"' not in page
    assert settings.api_token == "t"
    assert "=t\n" not in page and ">t<" not in page
    assert 'data-copy-target="vf_env"' in page and '<pre class="pre" id="vf_env">' in page
    assert 'export VERIFAIR_API_TOKEN="&lt;your token&gt;"' in page
    assert 'aria-label="Copy environment variables"' in page
    assert "Ask your agent to publish a verifair review, then reload this page." in page
    assert 'class="waiting"' not in page
    shared = shared_client.get("/").text
    assert "Welcome to verifair" in shared and "Create an agent token" in shared
    assert 'class="btn p" href="/access/tokens"' in shared

    client.cookies.clear()
    push(client)
    sign(client)
    client.cookies.set("vf_name", "Reviewer")
    approved = client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve"})
    assert approved.status_code == 200
    caught_up = client.get("/").text
    assert "You're all caught up" in caught_up and "Welcome to verifair" not in caught_up
    assert 'href="/changes"' in caught_up and 'href="/changes?scope=author"' in caught_up
    assert "0 need you · 0 waiting on others" in caught_up


def test_report_page_renders_sections_nav_links_and_sets_id_cookie(client):
    push(
        client,
        body="## Intent\nFull **new** price.\n\n## Edge cases\nFirst day.",
        links=[{"label": "code", "url": "https://x/mr/1"}],
    )
    r = client.get("/r/prorate")
    assert r.status_code == 200 and "vf_id" in r.cookies
    assert (
        'id="intent"' in r.text and 'id="edge-cases"' in r.text and "<strong>new</strong>" in r.text
    )
    assert r.text.index("BILL-412") < r.text.index("https://x/mr/1")
    assert 'href="#edge-cases"' in r.text
    assert 'class="badge b-unsigned"' in r.text and "High risk" in r.text
    assert client.get("/r/nope").status_code == 404


def test_review_page_sits_in_the_drawer_shell(client):
    push(client)
    sign(client)
    page = client.get("/r/prorate").text
    assert 'class="app drawer"' in page and 'popovertarget="vf_nav"' in page
    crumbs = page.split('class="crumbs"')[1].split("</nav>")[0]
    assert 'href="/p/billing-service"' in crumbs and '<span class="cur">Prorate</span>' in crumbs
    nav_link = re.search(r'<a [^>]*href="/p/billing-service"[^>]*>', page.split("</nav>")[0])
    assert 'aria-current="page"' in nav_link.group(0)
    assert page.index('<main class="main" id="vf_main">') < page.index('id="vf_verdict"')
    assert page.count("<main") == 1
    assert '<body class="has-verdict">' in page
    push(client, id="prorate")
    assert "has-verdict" not in client.get("/r/prorate/v/1").text


def test_review_page_uses_icons_not_emoji(client):
    push(client)
    client.post(
        "/api/reports/prorate/comments",
        json={"body": "Agent note", "section": "Intent"},
        headers=H,
    )
    [thread] = api_threads(client)
    client.cookies.set("vf_name", "Alice")
    page = client.get("/r/prorate").text
    comment = client.post(
        "/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"}
    )
    reply = client.post(
        f"/r/prorate/comments/{thread['id']}/reply", data={"version": 1, "body": "Thanks"}
    )
    resolved = client.post(f"/r/prorate/comments/{thread['id']}/resolve", data={"version": 1})
    assert comment.status_code == reply.status_code == resolved.status_code == 200
    assert 'class="thread resolved"' in resolved.text
    for text in (page, comment.text, reply.text, resolved.text):
        assert not {"💬", "🤖", "✓", "↗"} & set(text)
    assert "/static/icons.svg#message-square" in page and "/static/icons.svg#bot" in page
    assert "/static/icons.svg#bot" in reply.text
    assert (
        '<span class="ok"><svg class="ic14" aria-hidden="true"><use href="/static/icons.svg#check"'
        in (resolved.text)
    )


def test_css_has_no_old_alias_tokens():
    css = (Path(web.__file__).parent / "static" / "app.css").read_text()
    assert "--foreground" not in css and "--card:" not in css
    assert "--warning" not in css and "--muted-foreground" not in css
    assert "body.has-verdict" in css and ".app-main > .name-prompt { position: fixed;" in css


def _media(css, query):
    return "\n".join(part.split("\n}")[0] for part in css.split(f"@media ({query}) {{")[1:])


def test_css_colours_are_tokens():
    css = (Path(web.__file__).parent / "static" / "app.css").read_text()
    rest = re.sub(r":root[^{]*\{[^}]*\}", "", css)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", rest)
    assert "rgb(" not in rest and "rgba(" not in rest


def test_mobile_rules_exist():
    css = (Path(web.__file__).parent / "static" / "app.css").read_text()
    tablet = _media(css, "max-width: 1023px")
    assert ".app-nav:not(:popover-open)" in tablet and ".chg" in tablet
    assert ".grp-h .hint { display: none; }" in tablet
    phone = _media(css, "max-width: 640px")
    assert "content: attr(data-label)" in phone and ".top-act .btn .lbl" in phone
    assert "animation" not in css and "@keyframes" not in css


def test_env_block_wraps_clear_of_the_copy_button():
    css = (Path(web.__file__).parent / "static" / "app.css").read_text()
    pre = css.split("\n.pre {")[1].split("}")[0]
    assert "border-right: 40px solid var(--code-bg)" in pre and "overflow-x: auto" in pre


def test_review_rail_leaves_room_for_the_verdict_bar():
    css = (Path(web.__file__).parent / "static" / "app.css").read_text()
    assert "calc(100vh - 48px - 65px)" not in css
    assert "body.has-verdict { padding-bottom" not in css
    assert "body.has-verdict .side { padding-bottom: 112px; }" in css


def test_comment_without_name_returns_prompt_and_stores_nothing(client):
    push(client)
    r = client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"})
    assert r.status_code == 200
    assert "Set your name" in r.text and 'action="/me"' in r.text
    assert api_threads(client) == []


def test_comment_reply_resolve_via_htmx(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    r = client.post(
        "/r/prorate/comments",
        data={
            "version": 1,
            "section": "Edge cases",
            "quote": "first day",
            "body": "Which first day?",
        },
    )
    assert r.status_code == 200
    [thread] = api_threads(client)
    cid = thread["id"]
    assert (thread["author"], thread["section"], thread["quote"]) == (
        "Alice",
        "Edge cases",
        "first day",
    )
    assert f'id="vf_t{cid}"' in r.text and "first day" in r.text and "Which first day?" in r.text
    assert 'data-found="true"' in r.text and 'data-section-slug="edge-cases"' in r.text

    reply = client.post(
        f"/r/prorate/comments/{cid}/reply", data={"version": 1, "body": "Cycle start."}
    )
    assert "Which first day?" in reply.text and "Cycle start." in reply.text
    assert len(api_threads(client)[0]["replies"]) == 1

    resolved = client.post(f"/r/prorate/comments/{cid}/resolve", data={"version": 1})
    assert 'class="thread resolved"' in resolved.text
    assert api_threads(client)[0]["resolved"] is True

    page = client.get("/r/prorate")
    assert 'class="thread resolved"' in page.text and "0 open threads" in page.text
    assert (
        client.post("/r/prorate/comments/999/reply", data={"version": 1, "body": "x"}).status_code
        == 404
    )
    assert client.post("/r/prorate/comments/999/resolve").status_code == 404


def test_verdict_request_changes_refused_without_summary_or_thread(client):
    push(client)
    sign(client)
    client.cookies.set("vf_name", "Alice")
    r = client.post(
        "/r/prorate/verdict", data={"version": 1, "verdict": "request_changes", "summary": " "}
    )
    assert r.status_code == 200
    assert REFUSED_COPY in r.text
    g = client.get("/api/reports/prorate", headers=H).json()
    assert (g["status"], g["verdicts"]) == ("awaiting_review", [])
    assert (
        client.post("/r/prorate/verdict", data={"version": 1, "verdict": "maybe"}).status_code
        == 422
    )


def test_verdict_request_changes_allowed_with_summary(client):
    push(client)
    sign(client)
    client.cookies.set("vf_name", "Alice")
    r = client.post(
        "/r/prorate/verdict",
        data={"version": 1, "verdict": "request_changes", "summary": "Round half-up."},
    )
    assert "You requested changes on v1" in r.text
    assert client.get("/api/reports/prorate", headers=H).json()["status"] == "changes_requested"


def test_verdict_approve_changes_status_and_sidebar(client):
    push(client)
    sign(client)
    client.cookies.set("vf_name", "Alice")
    r = client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve"})
    assert r.status_code == 200
    assert "You approved v1" in r.text
    assert 'id="vf_sidebar"' in r.text and 'id="vf_status"' in r.text
    assert 'hx-swap-oob="true"' in r.text
    assert "Approved" in r.text and "approved v1" in r.text
    assert client.get("/api/reports/prorate", headers=H).json()["status"] == "approved"
    page = client.get("/r/prorate")
    assert "You approved v1" in page.text and "b-approved" in page.text


def test_old_version_is_read_only(client):
    push(client)
    push(client, id="prorate", body="## Intent\nChanged.")
    r = client.get("/r/prorate/v/1")
    assert r.status_code == 200
    assert "Viewing v1 · current is v2" in r.text and "Full new price." in r.text
    assert 'hx-post="/r/prorate/verdict"' not in r.text and "hx-post" not in r.text
    assert 'hx-post="/r/prorate/verdict"' in client.get("/r/prorate").text
    assert client.get("/r/prorate/v/9").status_code == 404


def test_comment_escapes_html(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    r = client.post(
        "/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "<b>x</b>"}
    )
    assert "&lt;b&gt;x&lt;/b&gt;" in r.text and "<b>x</b>" not in r.text
    assert "&lt;b&gt;x&lt;/b&gt;" in client.get("/r/prorate").text


def test_web_posts_notify(settings, make_app, monkeypatch):
    from dataclasses import replace

    from verifair import notify

    sent = []
    monkeypatch.setattr(notify, "send", lambda settings, text: sent.append(text))
    settings = replace(settings, slack_webhook_url="https://hooks/x")
    client = TestClient(make_app(settings), headers={"Sec-Fetch-Site": "same-origin"})
    push(client)
    assert sent[0] == "Draft report v1 in billing-service: Prorate http://localhost:8000/r/prorate"
    sign(client)
    assert sent[1] == (
        "Ready for review: Prorate v1 (risk high), signed by Signer http://localhost:8000/r/prorate"
    )
    client.cookies.set("vf_name", "Alice")
    client.post(
        "/r/prorate/comments",
        data={"version": 1, "section": "Intent", "body": "Why?", "blocking": "0"},
    )
    client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve", "summary": "Fine."})
    assert sent[2].startswith("Alice commented on Prorate › Intent: Why?")
    assert sent[3].startswith("Alice approved Prorate v1: Fine.")


def test_set_name(client):
    r = client.post("/me", data={"name": "  Alice ", "next": "/r/prorate"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/r/prorate")
    assert r.cookies["vf_name"] == "Alice"
    evil = client.post("/me", data={"name": "Bob", "next": "//evil"}, follow_redirects=False)
    assert evil.headers["location"] == "/"
    slash = client.post("/me", data={"name": "Bob", "next": "/\\evil"}, follow_redirects=False)
    assert slash.headers["location"] == "/"


def test_headings_cannot_collide_with_app_ids(client):
    headings = ["Intent", "Verdict", "Sidebar", "Popover", "T1", "Cform Intent", "Threads Intent"]
    push(client, body="\n\n".join(f"## {h}\ntext" for h in headings))
    client.cookies.set("vf_name", "Alice")
    thread = client.post(
        "/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"}
    )
    assert 'id="vf_t' in thread.text and 'id="vf_c' in thread.text
    r = client.get("/r/prorate")
    for slug in ["verdict", "sidebar", "popover", "t1", "cform-intent", "threads-intent"]:
        assert r.text.count(f'id="{slug}"') == 1, slug
    assert 'hx-post="/r/prorate/verdict"' in r.text and 'id="vf_verdict"' in r.text
    assert 'id="vf_cform_intent"' in r.text and 'hx-target="#vf_threads_intent"' in r.text


def test_blank_comment_and_reply_422(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    blank = client.post(
        "/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "  \n "}
    )
    assert blank.status_code == 422 and api_threads(client) == []
    client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"})
    cid = api_threads(client)[0]["id"]
    assert (
        client.post(
            f"/r/prorate/comments/{cid}/reply", data={"version": 1, "body": " "}
        ).status_code
        == 422
    )
    assert api_threads(client)[0]["replies"] == []


def test_name_form_next_keeps_query(client):
    push(client)
    r = client.get("/p/billing-service?tab=approved")
    assert 'name="next" value="/p/billing-service?tab=approved"' in r.text


def test_comment_time_is_labelled_utc(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    r = client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"})
    assert re.search(r'class="when">[^<]* \d\d:\d\d UTC<', r.text)


def test_first_visit_shows_inline_name_prompt(client):
    push(client)
    push(client, id="prorate", body="## Intent\nChanged.")
    r = client.get("/r/prorate")
    assert "Set your name</b>" in r.text
    assert r.text.index('<main class="main" id="vf_main">') < r.text.index('class="name-prompt"')
    assert 'name="next" value="/r/prorate"' in r.text
    assert 'class="name-prompt"' not in client.get("/r/prorate/v/1").text
    client.cookies.set("vf_name", "Alice")
    assert 'class="name-prompt"' not in client.get("/r/prorate").text


@pytest.mark.parametrize("name", ["  ", "x" * 61, "Al\x00ice"])
def test_set_name_invalid_redirects_back_with_error(client, name):
    push(client)
    r = client.post("/me", data={"name": name, "next": "/r/prorate"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/r/prorate?name_error=1")
    assert "vf_name" not in r.cookies
    page = client.get(r.headers["location"])
    assert page.text.count("Enter a name (1–60 characters).") == 1
    assert 'class="name-prompt"' in page.text
    assert 'name="next" value="/r/prorate"' in page.text


def test_set_name_invalid_keeps_query_and_shows_error_in_menu(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    nxt = "/p/billing-service?tab=approved"
    r = client.post("/me", data={"name": " ", "next": nxt}, follow_redirects=False)
    assert r.headers["location"] == nxt + "&name_error=1"
    page = client.get(r.headers["location"])
    assert '<details class="me" open>' in page.text
    assert page.text.count("Enter a name (1–60 characters).") == 1
    assert f'name="next" value="{nxt}"' in page.text


def test_request_changes_gate_counts_own_thread_carried_from_older_version(client):
    push(client)
    client.cookies.set("vf_id", "alice000")
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"})
    push(client, id="prorate", body="## Intent\nChanged.")
    sign(client, version=2)

    client.cookies.set("vf_id", "bob00000")
    client.cookies.set("vf_name", "Bob")
    refused = client.post("/r/prorate/verdict", data={"version": 2, "verdict": "request_changes"})
    assert REFUSED_COPY in refused.text
    assert client.get("/api/reports/prorate", headers=H).json()["verdicts"] == []

    client.cookies.set("vf_id", "alice000")
    client.cookies.set("vf_name", "Alice")
    allowed = client.post("/r/prorate/verdict", data={"version": 2, "verdict": "request_changes"})
    assert "You requested changes on v2" in allowed.text
    g = client.get("/api/reports/prorate", headers=H).json()
    assert (g["status"], g["verdicts"][0]["version"]) == ("changes_requested", 2)


def test_htmx_comment_without_name_retargets_form_and_stores_nothing(client):
    push(client)
    r = client.post(
        "/r/prorate/comments",
        data={"version": 1, "section": "Intent", "body": "Why?"},
        headers={"HX-Request": "true", "HX-Trigger": "vf_cform_intent"},
    )
    assert r.headers["HX-Retarget"] == "#vf_cform_intent"
    assert r.headers["HX-Reswap"] == "outerHTML"
    assert 'class="name-prompt"' in r.text
    assert api_threads(client) == []


STALE_COPY = "A newer version of this report was pushed. Reload to review v2."


def test_writes_from_an_outdated_page_store_nothing(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"})
    cid = api_threads(client)[0]["id"]
    push(client, id="prorate", body="## Intent\nChanged.")

    comment = client.post(
        "/r/prorate/comments",
        data={"version": 1, "section": "Intent", "body": "Late"},
        headers={"HX-Request": "true", "HX-Trigger": "vf_cform_intent"},
    )
    assert comment.status_code == 200 and STALE_COPY in comment.text
    assert 'href="/r/prorate"' in comment.text
    assert comment.headers["HX-Retarget"] == "#vf_cform_intent"
    reply = client.post(f"/r/prorate/comments/{cid}/reply", data={"version": 1, "body": "Late"})
    assert STALE_COPY in reply.text
    verdict = client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve"})
    assert verdict.status_code == 200 and STALE_COPY in verdict.text
    refused = client.post("/r/prorate/verdict", data={"version": 1, "verdict": "request_changes"})
    assert STALE_COPY in refused.text

    [thread] = api_threads(client)
    assert thread["replies"] == []
    assert client.get("/api/reports/prorate", headers=H).json()["verdicts"] == []
    assert client.post(f"/r/prorate/comments/{cid}/resolve").status_code == 200
    assert api_threads(client)[0]["resolved"] is True


def test_forms_carry_the_page_version(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"})
    page = client.get("/r/prorate").text
    assert page.count('<input type="hidden" name="version" value="1">') == 7
    missing = client.post("/r/prorate/verdict", data={"verdict": "approve"})
    assert missing.status_code == 422


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/r/prorate/comments", {"section": "Intent", "body": "x" * 20001}),
        ("/r/prorate/comments", {"section": "Intent", "body": "x", "quote": "q" * 2001}),
        ("/r/prorate/comments", {"section": "s" * 2001, "body": "x"}),
        ("/r/prorate/verdict", {"verdict": "approve", "summary": "s" * 2001}),
    ],
)
def test_web_form_limits_422(client, path, data):
    push(client)
    client.cookies.set("vf_name", "Alice")
    assert client.post(path, data={"version": 1} | data).status_code == 422
    assert api_threads(client) == []
    assert client.get("/api/reports/prorate", headers=H).json()["verdicts"] == []


def test_comment_response_resets_the_form_without_inline_js(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    r = client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"})
    assert re.search(r'<form class="cform" id="vf_cform_intent" hidden hx-swap-oob="true"', r.text)
    assert "hx-on" not in r.text and "hx-on" not in client.get("/r/prorate").text


def test_resolve_needs_confirm_and_reply_reopens(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"})
    cid = api_threads(client)[0]["id"]
    assert (
        'hx-confirm="Resolve this thread? You can reopen it later."'
        in client.get("/r/prorate").text
    )
    client.post(f"/r/prorate/comments/{cid}/resolve", data={"version": 1})
    again = client.post(f"/r/prorate/comments/{cid}/resolve", data={"version": 1})
    assert again.status_code == 200 and 'class="thread resolved"' in again.text
    reopened = client.post(f"/r/prorate/comments/{cid}/reply", data={"version": 1, "body": "No."})
    assert 'class="thread resolved"' not in reopened.text and "No." in reopened.text
    assert api_threads(client)[0]["resolved"] is False


def _form_of(page, button_id):
    return next(f for f in re.findall(r"<form\b.*?</form>", page, re.S) if f'id="{button_id}"' in f)


def _submit_without_js(client, page, button_id):
    form = _form_of(page, button_id)
    action = re.search(r'action="([^"]+)"', form)[1]
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form))
    return client.post(action, data=fields, headers={"HX-Request": ""}, follow_redirects=False)


def test_moderation_buttons_work_without_javascript(client):
    push(client)
    cid = _open_thread(client)
    page = client.get("/r/prorate").text
    r = _submit_without_js(client, page, f"vf_blocking_{cid}")
    assert (r.status_code, r.headers["location"]) == (303, f"/r/prorate#vf_t{cid}")
    assert api_threads(client)[0]["blocking"] is False
    r = _submit_without_js(client, client.get("/r/prorate").text, f"vf_resolve_{cid}")
    assert r.status_code == 303 and api_threads(client)[0]["resolved"] is True
    r = _submit_without_js(client, client.get("/r/prorate").text, f"vf_reopen_{cid}")
    assert r.status_code == 303 and api_threads(client)[0]["resolved"] is False


def test_resolve_without_name_retargets_thread_card(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"})
    cid = api_threads(client)[0]["id"]
    client.cookies.delete("vf_name")
    r = client.post(
        f"/r/prorate/comments/{cid}/resolve",
        headers={"HX-Request": "true", "HX-Trigger": f"vf_resolve_{cid}"},
    )
    assert r.headers["HX-Retarget"] == f"#vf_t{cid}"
    assert api_threads(client)[0]["resolved"] is False


def test_sidebar_marks_stale_verdicts_and_shows_summary(client):
    push(client)
    sign(client)
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve", "summary": "Fine."})
    current = client.get("/r/prorate").text
    assert "b-stale" not in current and "Fine." in current
    push(client, id="prorate", body="## Intent\nChanged.")
    page = client.get("/r/prorate").text
    assert 'class="badge b-approved b-stale"' in page and "approved v1" in page
    assert "Fine." in page
    old = client.get("/r/prorate/v/1").text
    assert ">Current: Unsigned<" in old
    assert ">Unsigned<" in page and "Current: " not in page


def test_report_page_a11y_attributes(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"})
    page = client.get("/r/prorate").text
    assert 'aria-label="Comment on Intent"' in page and 'aria-label="Comment on Edge cases"' in page
    assert 'aria-label="Reply"' in page and 'aria-label="Summary for the author"' in page
    assert 'aria-live="polite"' in page
    assert '<span class="n" role="img" aria-label="1 open thread">1</span>' in page
    assert page.count('hx-disabled-elt="find button"') == 4
    assert 'hx-disabled-elt="#vf_verdict button"' in page


@pytest.mark.parametrize("bad", ["agent", "short", "x" * 17, "bad id!!", "a.b.c.d.e"])
def test_invalid_or_agent_id_cookie_is_replaced(client, bad):
    push(client)
    client.cookies.set("vf_id", bad)
    client.cookies.set("vf_name", "Mallory")
    r = client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Hi"})
    assert r.cookies["vf_id"] != bad
    [thread] = api_threads(client)
    assert thread["author_id"] == r.cookies["vf_id"] and thread["author_type"] == "human"


def test_valid_id_cookie_is_kept(client):
    push(client)
    client.cookies.set("vf_id", "Ab_-1234")
    assert "vf_id" not in client.get("/r/prorate").cookies


def test_general_and_orphaned_threads_render_in_their_own_blocks(client):
    push(client)
    client.post("/api/reports/prorate/comments", json={"body": "General note"}, headers=H)
    client.post(
        "/api/reports/prorate/comments",
        json={"body": "Gone note", "section": "Edge cases"},
        headers=H,
    )
    push(client, id="prorate", body="## Intent\nFull new price.")
    page = client.get("/r/prorate").text
    general = page.index('id="vf_general"')
    earlier = page.index('id="vf_earlier"')
    assert page.index('id="intent"') < general < page.index("General note") < earlier
    assert earlier < page.index("Gone note")
    assert "General comments" in page
    assert 'href="#vf_general"' in page and 'href="#vf_earlier"' in page


def _resolved_thread(client, body="Why?"):
    push(client)
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": body})
    cid = api_threads(client)[0]["id"]
    client.post(f"/r/prorate/comments/{cid}/reply", data={"version": 1, "body": "A reply"})
    client.post(f"/r/prorate/comments/{cid}/resolve")
    return cid


def test_resolved_thread_expands_and_can_be_reopened(client):
    long_body = "word " * 40
    cid = _resolved_thread(client, long_body)
    page = client.get("/r/prorate").text
    assert f'<details class="thread resolved" id="vf_t{cid}"' in page
    assert long_body.strip() in page and "A reply" in page
    assert (
        f'hx-post="/r/prorate/comments/{cid}/reopen" hx-target="#vf_t{cid}" hx-swap="outerHTML"'
        in page
    )
    r = client.post(f"/r/prorate/comments/{cid}/reopen", data={"version": 1})
    assert r.status_code == 200
    assert 'class="thread resolved"' not in r.text and f'id="vf_t{cid}"' in r.text
    assert api_threads(client)[0]["resolved"] is False
    assert client.post("/r/prorate/comments/999/reopen").status_code == 404


def test_reopen_without_name_prompts_and_keeps_thread_resolved(client):
    cid = _resolved_thread(client)
    client.cookies.delete("vf_name")
    r = client.post(
        f"/r/prorate/comments/{cid}/reopen",
        headers={"HX-Request": "true", "HX-Trigger": f"vf_reopen_{cid}"},
    )
    assert 'class="name-prompt"' in r.text
    assert r.headers["HX-Retarget"] == f"#vf_t{cid}"
    assert api_threads(client)[0]["resolved"] is True


def test_verdict_messages_are_alerts(client):
    push(client)
    sign(client)
    client.cookies.set("vf_name", "Alice")
    refused = client.post("/r/prorate/verdict", data={"version": 1, "verdict": "request_changes"})
    assert f'<p class="error" role="alert">{REFUSED_COPY}</p>' in refused.text
    done = client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve"})
    assert '<p class="mine" role="alert">You approved v1</p>' in done.text


def test_app_js_caps_quote_and_maps_error_copy():
    from pathlib import Path

    import verifair

    js = (Path(verifair.__file__).parent / "static" / "app.js").read_text()
    assert "Array.from(quote).slice(0, QUOTE_MAX)" in js and "const QUOTE_MAX = 2000;" in js
    for copy in [
        "Check the text and try again.",
        "This item no longer exists. Reload the page.",
        "Connection lost. Your text is still here, try again.",
        "Something went wrong (${status}). Reload and try again.",
        "Your access changed. Reload the page.",
    ]:
        assert copy in js


@pytest.fixture
def proxied(settings, make_app, monkeypatch):
    from dataclasses import replace

    from verifair import db

    shared = replace(settings, **OIDC, bootstrap_admin_id="boss@example.com")
    app = make_app(shared)
    conn = db.connect(shared.database_url)
    for id in ("alice@example.com", "bob@example.com"):
        db.add_user(conn, id=id, name=id.partition("@")[0], role="member")
    token, _ = db.create_token(conn, owner_id="alice@example.com", label="agent")
    conn.close()
    monkeypatch.setitem(H, "Authorization", f"Bearer {token}")
    return SessionClient(app, headers={"Sec-Fetch-Site": "same-origin"})


def test_shared_mode_is_the_user(proxied):
    push(proxied)
    who = signed_in("alice@example.com")
    page = proxied.get("/r/prorate", headers=who)
    assert "Set your name" not in page.text and 'action="/me"' not in page.text
    assert '<span class="me" title="alice">alice</span>' in page.text
    assert "set-cookie" not in page.headers

    r = proxied.post(
        "/r/prorate/comments",
        data={"version": 1, "section": "Intent", "body": "Why?"},
        headers=who,
    )
    assert r.status_code == 200 and "Set your name" not in r.text and "alice" in r.text
    assert "set-cookie" not in r.headers
    thread = api_threads(proxied)[0]
    assert (thread["author_id"], thread["author"]) == ("alice@example.com", "alice")


def test_shared_mode_blocks_set_name(proxied):
    r = proxied.post(
        "/me",
        data={"name": "Mallory"},
        headers=signed_in("alice@example.com"),
        follow_redirects=False,
    )
    assert r.status_code == 403 and "vf_name" not in r.cookies


@pytest.mark.parametrize("session", [None, "", "x.y", "agent"])
def test_shared_mode_without_a_valid_session_cannot_act(proxied, session):
    push(proxied)
    proxied.cookies.set("vf_id", "bob00000")
    proxied.cookies.set("vf_name", "Bob")
    if session is None:
        headers = {}
    elif session == "agent":
        headers = signed_in("agent")
    else:
        headers = {"Cookie": f"vf_session={session}"}
    page = proxied.get("/r/prorate", headers=headers, follow_redirects=False)
    assert page.status_code == (403 if session == "agent" else 303)
    refused = 403 if session == "agent" else 401
    assert proxied.post("/me", data={"name": "Bob"}, headers=headers).status_code == refused
    r = proxied.post(
        "/r/prorate/comments",
        data={"version": 1, "section": "Intent", "body": "Why?"},
        headers=headers,
    )
    assert r.status_code == refused
    assert api_threads(proxied) == []


SIGN_IN_COPY = "Sign in to use verifair."
CSP = (
    "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline';"
    " base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


@pytest.fixture
def anonymous(shared_client):

    return TestClient(shared_client.app, follow_redirects=False)


@pytest.mark.parametrize(
    "path", ["/", "/p/x", "/r/x", "/docs", "/redoc", "/openapi.json", "/missing"]
)
def test_shared_mode_pages_without_a_session_go_to_sign_in(anonymous, path):
    r = anonymous.get(path)
    assert (r.status_code, r.headers["location"]) == (
        303,
        f"/login?next={url_quote(path, safe='')}",
    )
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in r.headers


@pytest.mark.parametrize(
    ("method", "path", "headers"),
    [
        ("get", "/r/x", {"HX-Request": "true"}),
        ("post", "/me", {}),
        ("post", "/r/x/comments", {}),
    ],
)
def test_shared_mode_htmx_and_posts_without_a_session_are_401(anonymous, method, path, headers):
    r = getattr(anonymous, method)(path, headers=headers)
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("text/html")
    assert SIGN_IN_COPY in r.text and 'href="/static/app.css"' in r.text
    assert r.headers["content-security-policy"] == CSP
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "set-cookie" not in r.headers


def test_shared_mode_non_members_are_403_and_deactivation_takes_effect(
    shared_client, shared_settings, as_user
):
    from verifair import db

    gone = as_user("gone@example.com")
    assert shared_client.get("/", headers=gone).status_code == 200
    conn = db.connect(shared_settings.database_url)
    db.update_user(conn, "gone@example.com", active=False)
    conn.close()
    for who in ["gone@example.com", "stranger@example.com", "<b>x</b>@example.com"]:
        for r in [
            shared_client.get("/", headers=signed_in(who)),
            shared_client.post("/me", data={"name": "M"}, headers=signed_in(who)),
        ]:
            assert r.status_code == 403
            assert r.headers["content-type"].startswith("text/html")
            assert r.headers["content-security-policy"] == CSP
            escaped = who.replace("<", "&lt;").replace(">", "&gt;")
            copy = f"You are not a member of this workspace yet. Ask an admin to add {escaped}."
            if who == "gone@example.com":
                copy = (
                    "Your access to this workspace was turned off."
                    " Ask an admin to reactivate gone@example.com."
                )
            assert copy in r.text
            assert "<b>" not in r.text


def test_shared_mode_email_is_matched_case_insensitively(anonymous, as_user, provider):
    as_user("bob@example.com")
    provider.sign_in(anonymous, email="BOB@Example.com")
    bob = anonymous.get("/")
    assert bob.status_code == 200 and '<span class="me" title="bob">bob</span>' in bob.text


def test_shared_mode_denials_are_logged_with_reason_and_path(
    anonymous, shared_client, shared_settings, as_user, caplog
):
    from verifair import db

    caplog.set_level("INFO", logger="verifair")
    as_user("gone@example.com")
    conn = db.connect(shared_settings.database_url)
    db.update_user(conn, "gone@example.com", active=False)
    conn.close()
    anonymous.get("/r/x")
    anonymous.get("/", headers={"Cookie": "vf_session=forged.session"})
    anonymous.get("/p/q", headers=signed_in("stranger@example.com"))
    anonymous.get("/access", headers=signed_in("gone@example.com"))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("membership ")]
    assert lines == [
        "membership denied reason=no-session path='/r/x'",
        "membership denied reason=bad-session path='/'",
        "membership denied reason=not-member path='/p/q'",
        "membership denied reason=inactive path='/access'",
    ]
    assert all(r.levelname == "INFO" for r in caplog.records if "membership" in r.getMessage())


def test_shared_mode_denial_log_escapes_control_characters(anonymous, caplog):
    caplog.set_level("INFO", logger="verifair")
    anonymous.get("/r/%1B%5B2K")
    assert "membership denied reason=no-session path='/r/\\x1b[2K'" in caplog.text
    assert "\x1b" not in caplog.text


def test_shared_mode_member_is_the_users_row_and_gets_no_cookies(
    shared_client, shared_settings, as_user
):
    from verifair import db

    bob = as_user("bob@example.com", name="Bob Builder")
    conn = db.connect(shared_settings.database_url)
    db.push_report(conn, project="p", title="T", risk="low", body="## A\nx")
    conn.close()
    shared_client.cookies.set("vf_id", "mallory0")
    shared_client.cookies.set("vf_name", "Mallory")
    for path in ["/", "/p/p", "/r/t", "/r/t/v/1", "/docs", "/openapi.json"]:
        r = shared_client.get(path, headers=bob)
        assert r.status_code == 200, path
        assert "set-cookie" not in r.headers, path
    page = shared_client.get("/r/t", headers=bob).text
    assert (
        '<span class="me" title="Bob Builder">Bob Builder</span>' in page and "Mallory" not in page
    )
    assert 'action="/me"' not in page
    r = shared_client.post(
        "/r/t/comments", data={"version": 1, "section": "A", "body": "Why?"}, headers=bob
    )
    assert r.status_code == 200 and "set-cookie" not in r.headers
    conn = db.connect(shared_settings.database_url)
    comment = db.list_comments(conn, "t")[0]
    conn.close()
    assert (comment["author_id"], comment["author_name"]) == ("bob@example.com", "Bob Builder")
    admin = shared_client.get("/").text
    assert '<span class="me" title="admin">admin</span>' in admin


def test_shared_mode_browser_check_still_applies_to_members(anonymous):
    r = anonymous.post("/me", data={"name": "M"}, headers=signed_in("admin@example.com"))
    assert (r.status_code, r.json()) == (403, {"detail": "browser request required"})


def test_shared_mode_health_static_and_api_skip_the_membership_check(anonymous):
    assert anonymous.get("/healthz").json() == {"ok": True}
    assert anonymous.get("/static/app.css").status_code == 200
    assert anonymous.get("/api/reports/x").json() == {"detail": "invalid token"}


UNSIGNED_COPY = "The author has not signed this version yet."
SELF_COPY = "You signed this report. Someone else has to approve it."


def _header(html):
    start = html.index('id="vf_header"')
    return html[start : html.index("</header>", start)]


def test_header_shows_merge_and_pull_request_links_only(client):
    push(
        client,
        links=[
            {"label": "CI run", "url": "https://ci.example.com/jobs/7"},
            {
                "label": "Merge request !231",
                "url": "https://gitlab.example.com/g/billing-service/-/merge_requests/231",
            },
            {"label": "PR <12>", "url": "https://github.com/o/verifair/pull/12/files"},
            {"label": "Bitbucket", "url": "https://bitbucket.org/o/web/pull-requests/3?tab=diff"},
            {"label": "Gitea", "url": "https://gitea.example.com/o/api/pulls/4#issuecomment-1"},
            {"label": "Pulls page", "url": "https://github.com/o/r/pulls"},
            {"label": "Fake", "url": "https://evil.example/?next=/pull/3"},
        ],
    )
    header = _header(client.get("/r/prorate").text)
    assert header.index("BILL-412") < header.index("billing-service!231")
    for url, short in (
        (
            "https://gitlab.example.com/g/billing-service/-/merge_requests/231",
            "billing-service!231",
        ),
        ("https://github.com/o/verifair/pull/12/files", "verifair#12"),
        ("https://bitbucket.org/o/web/pull-requests/3?tab=diff", "web#3"),
        ("https://gitea.example.com/o/api/pulls/4#issuecomment-1", "api#4"),
    ):
        assert f'href="{url}"' in header and f">{short}<" in header
    assert 'title="PR &lt;12&gt;"' in header and "PR <12>" not in header
    assert "ci.example.com" not in header and "evil.example" not in header
    assert 'href="https://github.com/o/r/pulls"' not in header
    page = client.get("/r/prorate").text
    links_line = page[page.index('<p class="links">') :]
    assert "https://ci.example.com/jobs/7" in links_line and "Merge request !231" in links_line


def test_header_code_links_ignore_spoofed_labels_and_duplicates(client):
    mr = "https://gitlab.example.com/g/p/-/merge_requests/9"
    push(
        client,
        ticket_url="https://github.com/o/r/pull/1",
        links=[
            {"label": "Signed by Security Team", "url": mr},
            {"label": "again", "url": mr},
            {"label": "BILL-412", "url": "https://github.com/o/r/pull/1"},
        ],
    )
    header = _header(client.get("/r/prorate").text)
    assert header.count(f'href="{mr}"') == 1 and ">p!9<" in header
    assert ">Signed by Security Team<" not in header
    assert header.count('href="https://github.com/o/r/pull/1"') == 1


def test_header_labels_only_use_plain_project_names(client):
    push(
        client,
        ticket_url="https://t/Signed by Security Team",
        links=[
            {
                "label": "a",
                "url": "https://gitlab.example.com/g/Signed by Security Team · 2026-11-01"
                "/-/merge_requests/9",
            },
            {
                "label": "b",
                "url": "https://gitlab.example.com/g/\u202e132!ecivres-gnillib/-/merge_requests/7",
            },
            {"label": "c", "url": "https://github.com/o/" + "x" * 101 + "/pull/3"},
            {"label": "d", "url": "https://gitlab.example.com/-/merge_requests/5"},
            {"label": "e", "url": "https://github.com/o/my.repo_2-x/pull/8"},
        ],
    )
    header = _header(client.get("/r/prorate").text)
    assert ">Signed by Security" not in header and "\u202e" not in header
    assert ">ticket<" in header and "x" * 101 not in header and ">-!5<" not in header
    assert ">my.repo_2-x#8<" in header and header.count('class="ext"') == 2


@pytest.mark.parametrize(
    ("ticket_url", "label"),
    [
        ("https://x.atlassian.net/browse/DATA-12?atlOrigin=abc", "DATA-12"),
        ("https://github.com/o/r/issues/12#issuecomment-99", "12"),
        ("https://gitlab.example.com/g/p/-/issues/7/", "7"),
        ("https://youtrack.example.com/issue/DATA-1", "DATA-1"),
        ("https://t/%E2%80%AEevil", "ticket"),
        ("https://t/" + "a" * 100, "a" * 100),
        ("https://t/" + "a" * 101, "ticket"),
    ],
)
def test_ticket_label_comes_from_the_url_path(ticket_url, label):
    assert web._ticket_label(ticket_url) == label


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.example.com/g/p/-/merge_requests/\u07c1\u07c2",
        "https://github.com/o/r/pull/\uff11\uff12",
        "https://github.com/o/r/pull/" + "1" * 11,
        "https://gitlab.example.com/g/%E2%80%AEp/-/merge_requests/9",
    ],
)
def test_code_links_need_plain_names_and_numbers(url):
    report = {"ticket_url": None}
    assert web._code_links(report, [("x", url)]) == []


def test_header_shows_one_link_per_merge_request(client):
    push(
        client,
        ticket_url="https://github.com/o/r/pull/1",
        links=[
            {"label": "MR", "url": "https://gitlab.example.com/g/p/-/merge_requests/9"},
            {"label": "MR diffs", "url": "https://gitlab.example.com/g/p/-/merge_requests/9/diffs"},
            {"label": "Same PR", "url": "https://github.com/o/r/pull/1/"},
            {"label": "Other host", "url": "https://gitlab.other.com/g/p/-/merge_requests/9"},
        ],
    )
    header = _header(client.get("/r/prorate").text)
    assert header.count(">p!9<") == 2 and "gitlab.other.com" in header
    assert ">r#1<" not in header


def test_header_keeps_code_links_after_signing_and_on_execution(client):
    push(client, links=[{"label": "MR", "url": "https://x/g/p/-/merge_requests/231"}])
    client.cookies.set("vf_name", "Signer")
    assert ">p!231<" in sign(client).text.split("</header>")[0]
    assert ">p!231<" in _header(client.get("/r/prorate?tab=execution").text)


def test_header_without_code_links_is_unchanged(client):
    push(client, links=[{"label": "Dashboard", "url": "https://grafana.example.com/d/1"}])
    header = _header(client.get("/r/prorate").text)
    assert header.count('class="ext"') == 1 and "grafana" not in header


def test_sign_moves_unsigned_to_awaiting_review(client):
    push(client)
    assert api_report(client)["status"] == "unsigned"
    assert "Sign this version" not in client.get("/r/prorate").text
    client.cookies.set("vf_name", "Signer")
    page = client.get("/r/prorate").text
    assert 'id="vf_header"' in page and "Sign this version" in page
    assert 'hx-post="/r/prorate/sign"' in page

    r = sign(client)
    assert r.status_code == 200
    assert r.text.startswith('<header class="head" id="vf_header">')
    assert "Signed by <b>Signer</b>" in r.text and "Sign this version" not in r.text
    assert '<aside class="side" id="vf_sidebar" hx-swap-oob="true">' in r.text
    assert 'hx-disabled-elt="#vf_verdict button" hx-swap-oob="true">' in r.text
    g = api_report(client)
    assert (g["status"], g["signed_by"]) == ("awaiting_review", "Signer")
    client.cookies.set("vf_name", "Alice")
    assert "Signed by <b>Signer</b>" in client.get("/r/prorate").text


def test_sign_needs_a_name_and_the_current_version(client):
    push(client)
    r = client.post(
        "/r/prorate/sign",
        data={"version": 1},
        headers={"HX-Request": "true", "HX-Trigger": "vf_sign"},
    )
    assert 'class="name-prompt"' in r.text and r.headers["HX-Retarget"] == "#vf_sign"
    assert api_report(client)["status"] == "unsigned"
    push(client, id="prorate", body="## Intent\nChanged.")
    client.cookies.set("vf_name", "Signer")
    stale = client.post("/r/prorate/sign", data={"version": 1})
    assert STALE_COPY in stale.text
    assert api_report(client)["signed_by"] is None
    assert client.post("/r/nope/sign", data={"version": 1}).status_code == 404


def test_verdict_refused_while_unsigned(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    approve = client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve"})
    assert f'<p class="error" role="alert">{UNSIGNED_COPY}</p>' in approve.text
    changes = client.post(
        "/r/prorate/verdict", data={"version": 1, "verdict": "request_changes", "summary": "No."}
    )
    assert UNSIGNED_COPY in changes.text
    g = api_report(client)
    assert (g["status"], g["verdicts"]) == ("unsigned", [])


def test_unsigned_version_shows_disabled_verdict_buttons_with_the_reason(client):
    push(client)
    client.cookies.set("vf_id", "review00")
    client.cookies.set("vf_name", "Alice")
    page = client.get("/r/prorate").text
    assert f'<p class="note">{UNSIGNED_COPY}</p>' in page
    assert '<div class="vmsg" tabindex="-1"' in page
    assert page.count(" disabled>") >= 3
    sign(client)
    client.cookies.set("vf_name", "Alice")
    page = client.get("/r/prorate").text
    assert UNSIGNED_COPY not in page
    assert 'value="approve">' in page and " disabled>" not in page


def test_signer_cannot_review_own_version(client):
    push(client)
    sign(client)
    client.cookies.set("vf_id", "signer00")
    client.cookies.set("vf_name", "Signer")
    approve = client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve"})
    assert f'<p class="error" role="alert">{SELF_COPY}</p>' in approve.text
    changes = client.post(
        "/r/prorate/verdict", data={"version": 1, "verdict": "request_changes", "summary": "No."}
    )
    assert SELF_COPY in changes.text
    g = api_report(client)
    assert (g["status"], g["verdicts"]) == ("awaiting_review", [])


def test_new_version_resets_signature(client):
    push(client)
    sign(client)
    push(client, id="prorate", body="## Intent\nChanged.")
    g = api_report(client)
    assert (g["status"], g["signed_by"], g["signed_at"]) == ("unsigned", None, None)
    client.cookies.set("vf_name", "Alice")
    page = client.get("/r/prorate").text
    assert "Signer" not in page and "Sign this version" in page
    assert 'class="badge b-unsigned"' in page
    assert "Sign this version" not in client.get("/r/prorate/v/1").text


def test_header_shows_provenance_and_versions_show_commit(client):
    push(
        client,
        agent="claude-code",
        model="opus",
        session_url="https://s/1",
        commit="0123456789abcdef",
    )
    page = client.get("/r/prorate").text
    assert "Written by <b>claude-code</b> (opus)" in page
    assert '<a class="ext" href="https://s/1">session<svg class="ic12"' in page
    assert "/static/icons.svg#external-link" in page
    assert "<code>0123456</code>" in page and "0123456789" not in page
    push(client, id="prorate")
    assert "Written by <b>claude-code</b> (opus)" in client.get("/r/prorate").text
    push(client, id="prorate", agent="claude-code")
    page = client.get("/r/prorate").text
    assert "Written by <b>claude-code</b><" in page and "session</a>" not in page
    push(client, id="prorate", agent=None)
    assert "Written by" not in client.get("/r/prorate").text


def test_phone_rows_say_status_and_risk(client):
    push(client)
    page = client.get("/changes?tab=all").text
    assert '<span class="chg-ph" aria-hidden="true">Unsigned · High risk · </span>' in page
    css = (Path(web.__file__).parent / "static" / "app.css").read_text()
    tablet = _media(css, "max-width: 1023px")
    assert ".chg-ph { display: inline; }" in tablet and ".chg-m { white-space: normal; }" in tablet


NO_JS = {"HX-Request": ""}


def test_forms_work_without_javascript(client):
    push(client)
    client.cookies.set("vf_id", "signer00")
    client.cookies.set("vf_name", "Signer")
    page = client.get("/r/prorate").text
    for action in ("sign", "comments"):
        assert f'method="post" action="/r/prorate/{action}"' in page
    r = client.post("/r/prorate/sign", data={"version": 1}, headers=NO_JS, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/r/prorate")
    client.cookies.set("vf_id", "review00")
    client.cookies.set("vf_name", "Rita")
    page = client.get("/r/prorate").text
    assert 'method="post" action="/r/prorate/verdict"' in page
    r = client.post(
        "/r/prorate/comments",
        data={"version": 1, "body": "Why day 20?"},
        headers=NO_JS,
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/r/prorate#vf_t")
    cid = int(r.headers["location"].rpartition("vf_t")[2])
    assert (
        f'method="post" action="/r/prorate/comments/{cid}/reply"' in client.get("/r/prorate").text
    )
    r = client.post(
        f"/r/prorate/comments/{cid}/reply",
        data={"version": 1, "body": "Because."},
        headers=NO_JS,
        follow_redirects=False,
    )
    assert (r.status_code, r.headers["location"]) == (303, f"/r/prorate#vf_t{cid}")
    data = {"version": 1, "verdict": "request_changes", "summary": "Day 20 is wrong."}
    r = client.post("/r/prorate/verdict", data=data, headers=NO_JS, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/r/prorate")
    assert api_report(client)["verdicts"][0]["summary"] == "Day 20 is wrong."


def test_refused_or_stale_form_without_javascript_is_an_error_not_a_fragment(client):
    push(client)
    sign(client)
    client.cookies.set("vf_name", "Rita")
    data = {"version": 1, "verdict": "request_changes"}
    r = client.post("/r/prorate/verdict", data=data, headers=NO_JS)
    assert r.status_code == 422 and "<h1>" in r.text and "Add a summary" in r.text
    assert '<a href="/r/prorate">Back to the report</a>' in r.text and "Back button" in r.text
    push(client, id="prorate")
    r = client.post("/r/prorate/verdict", data=data | {"summary": "x"}, headers=NO_JS)
    assert r.status_code == 409 and "Reload it and try again" in r.text
    r = client.post("/r/prorate/comments", data={"version": 2, "body": "  "}, headers=NO_JS)
    assert r.status_code == 422 and "comment body is empty" in r.text and "<h1>" in r.text
    client.cookies.delete("vf_name")
    r = client.post("/r/prorate/comments", data={"version": 2, "body": "x"}, headers=NO_JS)
    assert r.status_code == 422 and "Set your name" in r.text and "Back to the report" in r.text


def test_requested_changes_note_is_not_green(client):
    push(client)
    sign(client)
    client.cookies.set("vf_name", "Rita")
    data = {"version": 1, "verdict": "request_changes", "summary": "No."}
    page = client.post("/r/prorate/verdict", data=data, headers={"HX-Request": "true"}).text
    assert '<p class="mine no" role="alert">You requested changes on' in page
    css = (Path(web.__file__).parent / "static" / "app.css").read_text()
    assert ".verdict .mine.no { color: var(--s-changes); }" in css


def test_changes_counts_unsigned(client):
    push(client)
    push(client, title="Other")
    sign(client)
    page = client.get("/changes").text
    assert page.count('aria-label="Unsigned · 1"') == 1
    assert page.count('aria-label="Awaiting review · 1"') == 1


AUTHOR_COPY = "Only alice can sign this report."


def test_shared_mode_only_author_can_sign(proxied):
    push(proxied)
    bob = signed_in("bob@example.com")
    page = proxied.get("/r/prorate", headers=bob).text
    assert AUTHOR_COPY in page and "Sign this version" not in page
    r = proxied.post("/r/prorate/sign", data={"version": 1}, headers=bob)
    assert r.status_code == 403 and AUTHOR_COPY in r.text
    assert api_report(proxied)["status"] == "unsigned"

    alice = signed_in("alice@example.com")
    assert "Sign this version" in proxied.get("/r/prorate", headers=alice).text
    ok = proxied.post("/r/prorate/sign", data={"version": 1}, headers=alice)
    assert ok.status_code == 200 and "Signed by <b>alice</b>" in ok.text
    assert api_report(proxied)["status"] == "awaiting_review"
    refused = proxied.post(
        "/r/prorate/verdict", data={"version": 1, "verdict": "approve"}, headers=alice
    )
    assert SELF_COPY in refused.text
    proxied.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve"}, headers=bob)
    assert api_report(proxied)["status"] == "approved"


def test_shared_mode_cookie_user_may_not_sign(proxied):
    push(proxied)
    proxied.cookies.set("vf_name", "Bob")
    r = proxied.post("/r/prorate/sign", data={"version": 1})
    assert r.status_code == 401 and api_report(proxied)["status"] == "unsigned"


NO_OWNER_COPY = "This report has no owner. Push it again with an agent token under a new id."


@pytest.mark.parametrize("author", [None, "alice@example.com"])
def test_shared_mode_nobody_may_sign_a_report_without_owner(proxied, author):
    from verifair import db

    conn = db.connect(proxied.app.state.settings.database_url)
    db.push_report(
        conn, project="billing-service", title="Prorate", risk="high", body=SAMPLE, author=author
    )
    conn.close()
    for who in ("alice@example.com", "bob@example.com"):
        headers = signed_in(who)
        page = proxied.get("/r/prorate", headers=headers).text
        assert NO_OWNER_COPY in page and "Sign this version" not in page
        r = proxied.post("/r/prorate/sign", data={"version": 1}, headers=headers)
        assert r.status_code == 403 and NO_OWNER_COPY in r.text
    report = api_report(proxied)
    assert (report["status"], report["author_id"]) == ("unsigned", None)


def test_resigning_keeps_the_first_signer_and_does_not_notify(client, monkeypatch):
    from verifair import notify

    push(client)
    sign(client)
    sent = []
    monkeypatch.setattr(notify, "send", lambda settings, text: sent.append(text))
    client.cookies.set("vf_id", "bob00000")
    client.cookies.set("vf_name", "Bob")
    client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve"})
    sent.clear()
    r = client.post("/r/prorate/sign", data={"version": 1})
    assert r.status_code == 200 and "Signed by <b>Signer</b>" in r.text
    assert sent == []
    g = api_report(client)
    assert (g["status"], g["signed_by"]) == ("approved", "Signer")


BLOCKING_COPY = "Resolve the open blocking threads first, or mark them non-blocking."
SHARED_BLOCKING_COPY = (
    "Resolve the open blocking threads first. Ask the person who started each one, the report"
    " owner for agent threads, or an admin to resolve it or mark it non-blocking."
)
OWN_BLOCKING_COPY = "Resolve your open blocking threads first, or mark them non-blocking."


def _open_thread(client, blocking=None):
    client.cookies.set("vf_id", "alice000")
    client.cookies.set("vf_name", "Alice")
    data = {"version": 1, "section": "Intent", "body": "Why?"}
    if blocking is not None:
        data["blocking"] = blocking
    client.post("/r/prorate/comments", data=data)
    client.cookies.set("vf_id", "bob00000")
    client.cookies.set("vf_name", "Bob")
    return api_threads(client)[-1]["id"]


def _approve(client):
    return client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve"})


def test_approve_refused_while_blocking_thread_open_and_allowed_after_resolve(client):
    push(client)
    sign(client)
    cid = _open_thread(client)
    refused = _approve(client)
    assert f'<p class="error" role="alert">{BLOCKING_COPY}</p>' in refused.text
    assert api_report(client)["verdicts"] == []

    client.post(f"/r/prorate/comments/{cid}/resolve")
    assert "You approved v1" in _approve(client).text

    client.post(f"/r/prorate/comments/{cid}/reply", data={"version": 1, "body": "Still wrong."})
    assert BLOCKING_COPY in _approve(client).text


def test_local_mode_keeps_the_plain_blocked_copy_for_your_own_thread(client):
    push(client)
    sign(client)
    _open_thread(client)
    client.cookies.set("vf_id", "alice000")
    client.cookies.set("vf_name", "Alice")
    assert f'<p class="error" role="alert">{BLOCKING_COPY}</p>' in _approve(client).text


def test_approve_allowed_after_marking_thread_non_blocking(client):
    push(client)
    sign(client)
    cid = _open_thread(client)
    r = client.post(f"/r/prorate/comments/{cid}/blocking", data={"version": 1, "blocking": "0"})
    assert r.status_code == 200
    assert f'id="vf_t{cid}"' in r.text and 'class="badge tag-suggestion">Suggestion' in r.text
    assert "Mark blocking" in r.text and 'id="vf_sidebar"' in r.text
    assert 'id="vf_verdict"' not in r.text and r.text.count('hx-swap-oob="true"') >= 2
    assert "0 blocking · 1 suggestion" in r.text
    assert api_threads(client)[0]["blocking"] is False
    assert "You approved v1" in _approve(client).text

    back = client.post(f"/r/prorate/comments/{cid}/blocking", data={"version": 1, "blocking": "1"})
    assert 'class="badge tag-blocking">Blocking' in back.text and "Mark non-blocking" in back.text
    assert api_threads(client)[0]["blocking"] is True


def test_comment_form_non_blocking_checkbox_and_sidebar_counts(client):
    push(client)
    page = client.get("/r/prorate")
    assert 'name="blocking" value="0"' in page.text and "Non-blocking (suggestion)" in page.text
    _open_thread(client)
    _open_thread(client, blocking="0")
    _open_thread(client, blocking="0")
    threads = api_threads(client)
    assert [t["blocking"] for t in threads] == [True, False, False]
    page = client.get("/r/prorate")
    assert "1 blocking · 2 suggestions" in page.text and "3 open threads" in page.text
    assert page.text.count('class="badge tag-suggestion"') == 2
    client.post(f"/r/prorate/comments/{threads[0]['id']}/resolve")
    assert "0 blocking · 2 suggestions" in client.get("/r/prorate").text


def test_request_changes_refused_with_only_own_non_blocking_thread(client):
    push(client)
    sign(client)
    client.cookies.set("vf_name", "Alice")
    client.post(
        "/r/prorate/comments",
        data={"version": 1, "section": "Intent", "body": "Nit.", "blocking": "0"},
    )
    r = client.post("/r/prorate/verdict", data={"version": 1, "verdict": "request_changes"})
    assert REFUSED_COPY in r.text
    assert api_report(client)["verdicts"] == []


def test_blocking_toggle_root_only_and_needs_name(client):
    push(client)
    cid = _open_thread(client)
    reply = client.post(f"/r/prorate/comments/{cid}/reply", data={"version": 1, "body": "Hm."})
    assert reply.status_code == 200
    rid = api_threads(client)[0]["replies"][0]["id"]
    assert (
        client.post(
            f"/r/prorate/comments/{rid}/blocking", data={"version": 1, "blocking": "0"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/r/prorate/comments/999/blocking", data={"version": 1, "blocking": "0"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/r/prorate/comments/{cid}/blocking", data={"version": 1, "blocking": "x"}
        ).status_code
        == 422
    )

    client.cookies.delete("vf_name")
    r = client.post(
        f"/r/prorate/comments/{cid}/blocking",
        data={"version": 1, "blocking": "0"},
        headers={"HX-Request": "true"},
    )
    assert 'class="name-prompt"' in r.text and r.headers["HX-Retarget"] == f"#vf_t{cid}"
    assert api_threads(client)[0]["blocking"] is True


def test_api_non_blocking_thread_shows_suggestion_tag(client):
    push(client)
    client.post(
        "/api/reports/prorate/comments",
        json={"body": "Consider renaming.", "blocking": False},
        headers=H,
    )
    page = client.get("/r/prorate").text
    assert 'class="badge tag-suggestion">Suggestion' in page
    assert 'class="badge tag-blocking"' not in page


def test_comment_stores_prefix_suffix_and_marks_the_chosen_occurrence(client):
    push(client, body="## Intent\nthe fee is 5. later the fee is 6.")
    client.cookies.set("vf_name", "Alice")
    page = client.get("/r/prorate").text
    assert 'name="prefix" value=""' in page and 'name="suffix" value=""' in page
    data = {"version": 1, "section": "Intent", "quote": "the fee is", "body": "Which?"}
    r = client.post("/r/prorate/comments", data=data | {"prefix": "5. later ", "suffix": " 6."})
    assert 'data-match-start="20"' in r.text
    [thread] = api_threads(client)
    assert (thread["prefix"], thread["suffix"]) == ("5. later ", " 6.")
    too_long = client.post("/r/prorate/comments", data=data | {"prefix": "p" * 65})
    assert too_long.status_code == 422


def test_moved_thread_shows_where_it_came_from(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    data = {"version": 1, "section": "Intent", "quote": "Full new price.", "body": "Why?"}
    client.post("/r/prorate/comments", data=data)
    push(client, id="prorate", body="## Goal\nFull new price.")
    page = client.get("/r/prorate").text
    assert 'moved from "Intent"' in page and 'data-match-start="0"' in page
    assert 'data-found="true" data-section-slug="goal"' in page


@pytest.mark.parametrize("mutation", ["comment", "reopen", "blocking"])
def test_approval_rechecks_blockers_committed_before_insert(
    client, settings, monkeypatch, mutation
):
    from contextlib import closing

    from verifair import db

    push(client)
    sign(client)
    cid = _open_thread(client, blocking="0" if mutation == "blocking" else "1")
    if mutation != "blocking":
        client.post(f"/r/prorate/comments/{cid}/resolve")
    original = db.add_verdict

    def interleave(conn, **kwargs):
        with closing(db.connect(settings.database_url)) as other:
            if mutation == "comment":
                db.add_comment(
                    other,
                    report_id="prorate",
                    version=1,
                    author_id="carol000",
                    author_name="Carol",
                    body="A newly committed blocker",
                )
            elif mutation == "reopen":
                db.reopen_thread(other, "prorate", cid)
            else:
                db.set_blocking(other, "prorate", cid, True)
        return original(conn, **kwargs)

    monkeypatch.setattr(db, "add_verdict", interleave)
    response = client.post(
        "/r/prorate/verdict",
        data={"version": 1, "verdict": "approve", "summary": "Keep the race draft"},
    )
    assert BLOCKING_COPY in response.text
    assert "Keep the race draft</textarea>" in response.text
    assert api_report(client)["verdicts"] == []
    assert any(t["blocking"] and not t["resolved"] for t in api_threads(client))


def test_approval_holds_write_lock_during_validation(client, settings, monkeypatch):
    from contextlib import closing

    import psycopg

    from verifair import db, rules

    push(client)
    sign(client)
    client.cookies.set("vf_name", "Bob")
    original = rules.can_approve
    attempted = []

    def try_concurrent_writer(threads):
        with closing(db.connect(settings.database_url)) as other:
            other.execute("SET lock_timeout = '50ms'")
            try:
                db.add_comment(
                    other,
                    report_id="prorate",
                    version=1,
                    author_id="carol000",
                    author_name="Carol",
                    body="Concurrent blocker",
                )
            except psycopg.errors.LockNotAvailable:
                attempted.append("locked")
            else:
                attempted.append("committed")
        return original(threads)

    monkeypatch.setattr(rules, "can_approve", try_concurrent_writer)
    assert "You approved v1" in _approve(client).text
    assert attempted == ["locked"]
    assert api_threads(client) == []


def test_request_changes_rechecks_own_thread_before_insert(client, settings, monkeypatch):
    from contextlib import closing

    from verifair import db

    push(client)
    sign(client)
    cid = _open_thread(client)
    client.cookies.set("vf_id", "alice000")
    client.cookies.set("vf_name", "Alice")
    original = db.add_verdict

    def interleave(conn, **kwargs):
        with closing(db.connect(settings.database_url)) as other:
            db.resolve_thread(other, "prorate", cid)
        return original(conn, **kwargs)

    monkeypatch.setattr(db, "add_verdict", interleave)
    response = client.post("/r/prorate/verdict", data={"version": 1, "verdict": "request_changes"})
    assert REFUSED_COPY in response.text
    assert api_report(client)["verdicts"] == []


def test_stale_blocking_toggle_keeps_verdict_bound_to_viewed_version(client):
    push(client)
    sign(client)
    cid = _open_thread(client)
    page = client.get("/r/prorate").text
    assert 'name="version" value="1"' in page
    push(client, id="prorate", body="## Intent\nUnread second version")
    sign(client, version=2)
    client.cookies.set("vf_name", "Bob")
    response = client.post(
        f"/r/prorate/comments/{cid}/blocking",
        data={"version": 1, "blocking": "0"},
        headers={"HX-Trigger": f"vf_blocking_{cid}"},
    )
    assert STALE_COPY in response.text
    assert 'id="vf_verdict"' not in response.text
    assert 'name="version" value="2"' not in response.text
    assert api_threads(client)[0]["blocking"] is True
    assert STALE_COPY in _approve(client).text
    assert api_report(client)["verdicts"] == []
    form = _form_of(page, f"vf_blocking_{cid}")
    assert 'name="version" value="1"' in form and 'name="blocking" value="0"' in form


def test_blocking_toggle_checks_version_inside_write_transaction(client, settings, monkeypatch):
    from contextlib import closing

    from verifair import db

    push(client)
    cid = _open_thread(client)
    original = db.set_blocking

    def interleave(conn, *args, **kwargs):
        with closing(db.connect(settings.database_url)) as other:
            db.push_report(
                other,
                project="billing-service",
                title="Prorate",
                risk="high",
                body="## Intent\nUnread second version",
                id="prorate",
            )
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(db, "set_blocking", interleave)
    response = client.post(
        f"/r/prorate/comments/{cid}/blocking", data={"version": 1, "blocking": "0"}
    )
    assert STALE_COPY in response.text
    assert api_threads(client)[0]["blocking"] is True
    assert 'id="vf_verdict"' not in response.text


def test_blocking_toggle_preserves_draft_verdict_form(client):
    push(client)
    sign(client)
    cid = _open_thread(client)
    rejected = client.post(
        "/r/prorate/verdict",
        data={"version": 1, "verdict": "approve", "summary": "Keep this unsent draft"},
    )
    assert "Keep this unsent draft</textarea>" in rejected.text
    response = client.post(
        f"/r/prorate/comments/{cid}/blocking", data={"version": 1, "blocking": "0"}
    )
    assert response.status_code == 200
    assert 'id="vf_sidebar"' in response.text
    assert 'id="vf_verdict"' not in response.text
    assert 'name="summary"' not in response.text
    assert "You approved v1" in _approve(client).text


MODERATION_COPY = (
    "Only the person who started this thread, the report owner for agent threads, or an admin"
    " can do that."
)


@pytest.fixture
def moderation(shared_client, shared_settings, as_user):
    from verifair import db

    people = {
        "admin": signed_in("admin@example.com"),
        "owner": as_user("owner@example.com", name="Olga Owner"),
        "carol": as_user("carol@example.com", name="Carol Creator"),
        "dave": as_user("dave@example.com", name="Dave Other"),
    }
    conn = db.connect(shared_settings.database_url)
    db.push_report(
        conn,
        project="p",
        title="T",
        risk="low",
        body="## A\nx",
        id="r",
        author_id="owner@example.com",
    )
    human, agent = (
        db.add_comment(
            conn,
            report_id="r",
            version=1,
            author_id=author,
            author_name=author,
            body="Why?",
            section="A",
        )["id"]
        for author in ("carol@example.com", "agent")
    )
    conn.close()
    return people, human, agent


def _moderation_state(shared_settings, cid):
    from verifair import db

    conn = db.connect(shared_settings.database_url)
    row = conn.execute(
        "SELECT resolved_at IS NOT NULL, resolved_by, blocking FROM comments WHERE id=%s", (cid,)
    ).fetchone()
    conn.close()
    return tuple(row.values())


@pytest.mark.parametrize(
    ("who", "human_ok", "agent_ok"),
    [("carol", True, False), ("owner", False, True), ("admin", True, True), ("dave", False, False)],
)
def test_shared_mode_moderation_buttons_only_for_creator_admin_or_report_owner(
    shared_client, moderation, who, human_ok, agent_ok
):
    people, human, agent = moderation
    page = shared_client.get("/r/r", headers=people[who]).text
    for cid, ok in ((human, human_ok), (agent, agent_ok)):
        assert (f'id="vf_resolve_{cid}"' in page) is ok
        assert (f'id="vf_blocking_{cid}"' in page) is ok
        assert f'id="vf_reply_{cid}"' in page


def test_shared_mode_moderation_refused_for_other_member(
    shared_client, shared_settings, moderation
):
    people, human, agent = moderation
    dave = people["dave"]
    for cid in (human, agent):
        for path, data in [
            (f"/r/r/comments/{cid}/resolve", {"version": 1}),
            (f"/r/r/comments/{cid}/blocking", {"version": 1, "blocking": "0"}),
        ]:
            r = shared_client.post(path, data=data, headers=dave)
            assert (r.status_code, r.json()) == (403, {"detail": MODERATION_COPY}), path
            r = shared_client.post(path, data=data, headers=dave | {"HX-Request": "true"})
            assert r.status_code == 200, path
            assert f'<p class="error" role="alert">{MODERATION_COPY}</p>' in r.text
            assert f'id="vf_t{cid}"' in r.text and f'id="vf_reply_{cid}"' in r.text
            assert f'id="vf_resolve_{cid}"' not in r.text
    assert _moderation_state(shared_settings, human) == (0, None, 1)
    assert _moderation_state(shared_settings, agent) == (0, None, 1)
    assert shared_client.post("/r/r/comments/999/resolve", headers=dave).status_code == 404


def test_shared_mode_blocked_approval_names_who_can_resolve(shared_client, moderation):
    people, _, _ = moderation
    assert shared_client.post("/r/r/sign", data={"version": 1}, headers=people["owner"]).is_success
    r = shared_client.post(
        "/r/r/verdict",
        data={"version": 1, "verdict": "approve"},
        headers=people["dave"] | {"HX-Request": "true"},
    )
    assert f'<p class="error" role="alert">{SHARED_BLOCKING_COPY}</p>' in r.text
    assert BLOCKING_COPY not in r.text


def test_shared_mode_blocked_approval_on_your_own_threads_says_resolve_yours(
    shared_client, moderation
):
    people, _, agent = moderation
    shared_client.post("/r/r/sign", data={"version": 1}, headers=people["owner"])
    verdict = {"version": 1, "verdict": "approve"}
    r = shared_client.post(
        "/r/r/verdict", data=verdict, headers=people["carol"] | {"HX-Request": "true"}
    )
    assert f'<p class="error" role="alert">{SHARED_BLOCKING_COPY}</p>' in r.text
    shared_client.post(
        f"/r/r/comments/{agent}/resolve", headers=people["owner"] | {"HX-Request": "true"}
    )
    r = shared_client.post(
        "/r/r/verdict", data=verdict, headers=people["carol"] | {"HX-Request": "true"}
    )
    assert f'<p class="error" role="alert">{OWN_BLOCKING_COPY}</p>' in r.text
    r = shared_client.post(
        "/r/r/verdict", data=verdict, headers=people["dave"] | {"HX-Request": "true"}
    )
    assert f'<p class="error" role="alert">{SHARED_BLOCKING_COPY}</p>' in r.text


def test_shared_mode_refusal_on_resolved_thread_opens_the_card(shared_client, moderation):
    people, human, _ = moderation
    shared_client.post(f"/r/r/comments/{human}/resolve", headers=people["carol"])
    r = shared_client.post(
        f"/r/r/comments/{human}/blocking",
        data={"version": 1, "blocking": "0"},
        headers=people["dave"] | {"HX-Request": "true"},
    )
    assert (
        f'<details class="thread resolved" id="vf_t{human}" data-section-slug="a" open>' in r.text
    )
    assert f'<p class="error" role="alert">{MODERATION_COPY}</p>' in r.text


def test_shared_mode_creator_admin_and_report_owner_can_moderate(
    shared_client, shared_settings, moderation
):
    people, human, agent = moderation
    r = shared_client.post(
        f"/r/r/comments/{human}/blocking",
        data={"version": 1, "blocking": "0"},
        headers=people["admin"] | {"HX-Request": "true"},
    )
    assert r.status_code == 200 and "Mark blocking" in r.text
    r = shared_client.post(
        f"/r/r/comments/{human}/resolve",
        headers=people["carol"] | {"HX-Request": "true"},
        data={"version": 1},
    )
    assert r.status_code == 200 and 'class="thread resolved"' in r.text
    r = shared_client.post(
        f"/r/r/comments/{agent}/resolve",
        headers=people["owner"] | {"HX-Request": "true"},
        data={"version": 1},
    )
    assert r.status_code == 200 and 'class="thread resolved"' in r.text
    assert _moderation_state(shared_settings, human) == (1, "carol@example.com", 0)
    assert _moderation_state(shared_settings, agent) == (1, "owner@example.com", 1)

    r = shared_client.post(
        f"/r/r/comments/{agent}/reopen",
        headers=people["dave"] | {"HX-Request": "true"},
        data={"version": 1},
    )
    assert r.status_code == 200 and 'class="thread resolved"' not in r.text
    assert _moderation_state(shared_settings, agent) == (0, None, 1)


def test_resolved_summary_names_who_resolved(shared_client, shared_settings, moderation):
    from verifair import db

    people, human, agent = moderation
    conn = db.connect(shared_settings.database_url)
    token, _ = db.create_token(conn, owner_id="owner@example.com", label="agent")
    conn.close()
    shared_client.post(f"/r/r/comments/{human}/resolve", headers=people["carol"])
    r = shared_client.post(
        f"/api/reports/r/comments/{agent}/resolve", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.json() == {"resolved": True}
    page = shared_client.get("/r/r", headers=people["dave"]).text
    assert "Why? · resolved by <b>Carol Creator</b></summary>" in page
    assert "Why? · resolved by <b>agent</b></summary>" in page
    threads = shared_client.get(
        "/api/reports/r/comments", headers={"Authorization": f"Bearer {token}"}
    ).json()["threads"]
    assert {t["id"]: t["resolved_by"] for t in threads} == {
        human: "carol@example.com",
        agent: "agent",
    }
    assert all(reply["resolved_by"] == t["resolved_by"] for t in threads for reply in t["replies"])


def test_local_mode_anyone_moderates_and_resolver_is_not_shown(client):
    push(client)
    cid = _open_thread(client)
    page = client.get("/r/prorate").text
    assert f'id="vf_resolve_{cid}"' in page and f'id="vf_blocking_{cid}"' in page
    r = client.post(f"/r/prorate/comments/{cid}/resolve", data={"version": 1})
    assert r.status_code == 200 and 'class="thread resolved"' in r.text
    assert "resolved by" not in r.text and "resolved by" not in client.get("/r/prorate").text
    [thread] = api_threads(client)
    assert (thread["resolved"], thread["resolved_by"]) == (True, None)


def test_thread_markup_for_allowed_users_matches_local_mode(shared_settings, moderation):
    from verifair import db, web

    _, human, agent = moderation
    conn = db.connect(shared_settings.database_url)
    report = db.get_report(conn, "r")
    template = web.templates.get_template("_thread.html")

    def threads(user_id):
        member = db.get_user(conn, user_id)
        ctx = web.report_context(conn, report, 1, user_id, member, member is not None)
        return {t.root["id"]: template.render(ctx | {"thread": t}) for t in ctx["threads"]}

    local = threads("someone0")
    assert all(f'id="vf_resolve_{cid}"' in html for cid, html in local.items())
    assert threads("admin@example.com") == local
    assert threads("carol@example.com")[human] == local[human]
    assert threads("owner@example.com")[agent] == local[agent]
    conn.close()


REOPEN_CONFIRM = (
    ' hx-confirm="Reopen this thread? Only its starter or an admin can resolve it again."'
)
AGENT_REOPEN_CONFIRM = (
    ' hx-confirm="Reopen this thread? Only its starter, the report owner for agent threads,'
    ' or an admin can resolve it again."'
)


def test_reopen_asks_first_only_when_the_user_cannot_resolve_it_again(
    shared_client, shared_settings, moderation
):
    from verifair import db, web

    people, human, agent = moderation
    shared_client.post(f"/r/r/comments/{human}/resolve", headers=people["carol"])
    shared_client.post(f"/r/r/comments/{agent}/resolve", headers=people["owner"])

    def reopen(who, cid):
        page = shared_client.get("/r/r", headers=people[who]).text
        return re.search(rf'<button [^>]*id="vf_reopen_{cid}"[^>]*>', page).group(0)

    for who, human_ok, agent_ok in [
        ("dave", False, False),
        ("carol", True, False),
        ("owner", False, True),
        ("admin", True, True),
    ]:
        for cid, ok, confirm, other in (
            (human, human_ok, REOPEN_CONFIRM, AGENT_REOPEN_CONFIRM),
            (agent, agent_ok, AGENT_REOPEN_CONFIRM, REOPEN_CONFIRM),
        ):
            button = reopen(who, cid)
            assert (confirm in button) is not ok and other not in button, (who, cid)

    conn = db.connect(shared_settings.database_url)
    report = db.get_report(conn, "r")
    template = web.templates.get_template("_thread.html")

    def threads(user_id):
        member = db.get_user(conn, user_id)
        ctx = web.report_context(conn, report, 1, user_id, member, member is not None)
        return {t.root["id"]: template.render(ctx | {"thread": t}) for t in ctx["threads"]}

    admin, dave = threads("admin@example.com"), threads("dave@example.com")
    assert all('id="vf_reopen_' in html and "hx-confirm" not in html for html in admin.values())
    assert {
        cid: html.replace(REOPEN_CONFIRM, "").replace(AGENT_REOPEN_CONFIRM, "")
        for cid, html in dave.items()
    } == admin
    conn.close()


def test_shared_mode_reply_reopens_only_when_the_replier_may_moderate(
    shared_client, shared_settings, moderation
):
    from verifair import db

    people, human, agent = moderation
    conn = db.connect(shared_settings.database_url)
    token, _ = db.create_token(conn, owner_id="owner@example.com", label="agent")
    conn.close()
    owners_agent = {"Authorization": f"Bearer {token}"}

    def resolved(cid):
        return _moderation_state(shared_settings, cid)[0] == 1

    def web_reply(cid, who):
        r = shared_client.post(
            f"/r/r/comments/{cid}/reply", data={"version": 1, "body": "Fixed."}, headers=who
        )
        assert r.status_code == 200

    def api_reply(cid):
        r = shared_client.post(
            "/api/reports/r/comments",
            json={"body": "Fixed.", "parent_id": cid},
            headers=owners_agent,
        )
        assert r.status_code == 201

    for cid, reply, reopens in [
        (human, lambda: api_reply(human), False),
        (human, lambda: web_reply(human, people["owner"]), False),
        (human, lambda: web_reply(human, people["dave"]), False),
        (human, lambda: web_reply(human, people["carol"]), True),
        (human, lambda: web_reply(human, people["admin"]), True),
        (agent, lambda: api_reply(agent), True),
        (agent, lambda: web_reply(agent, people["owner"]), True),
        (agent, lambda: web_reply(agent, people["carol"]), False),
    ]:
        shared_client.post(f"/r/r/comments/{cid}/resolve", headers=people["admin"])
        assert resolved(cid)
        reply()
        assert resolved(cid) is not reopens, (cid, reopens)


SHELL_PAGES = ["/", "/p/billing-service", "/r/prorate"]


def test_shell_wraps_every_page(client):
    push(client)
    for path in SHELL_PAGES:
        page = client.get(path).text
        assert 'id="vf_nav" popover' in page, path
        assert 'popovertarget="vf_nav"' in page, path
        assert 'href="/changes?scope=reviewer"' in page, path
        assert 'href="https://romanzdk.github.io/verifair/"' in page, path
        assert f"v{importlib.metadata.version('verifair')}" in page, path
        assert ('class="app drawer"' in page) is (path == "/r/prorate"), path


def test_pages_have_no_inline_script(client, shared_client):
    push(client)
    responses = [client.get(path) for path in SHELL_PAGES] + [
        shared_client.get(path) for path in ("/access/tokens", "/access/members")
    ]
    for r in responses:
        assert r.status_code == 200, r.url
        assert re.search(r"<script(?![^>]*\ssrc=)", r.text) is None, r.url
        assert re.search(r"\son[a-z]+=", r.text) is None, r.url
        assert r.headers["content-security-policy"] == APP_CSP, r.url


def test_icon_sprite_has_every_icon_the_templates_use():
    root = Path(web.__file__).parent
    used = {
        name
        for template in (root / "templates").glob("*.html")
        for name in re.findall(r'icon\(\s*"([a-z0-9-]+)"(?!\s*~)', template.read_text())
    }
    sprite = (root / "static" / "icons.svg").read_text()
    assert {"menu", "inbox", "user-check", "folder", "copy", "book-open"} <= used
    for name in used | {"risk-low", "risk-medium", "risk-high"} | set(web.STATUS_ICONS.values()):
        assert f'id="{name}"' in sprite, name
    assert "ISC" in sprite


def test_icon_escapes_its_values():
    assert web.icon('a"b', "c<d") == (
        '<svg class="c&lt;d" aria-hidden="true"><use href="/static/icons.svg#a&#34;b"/></svg>'
    )


def test_user_block_local_and_shared(client, shared_client, as_user):
    r = client.get("/")
    page = r.text
    assert '<details class="me"' in page and 'action="/me"' in page
    assert f'data-copy-identity="{r.cookies["vf_id"]}"' in page
    assert (
        "Settings need shared mode with sign-in. Set VERIFAIR_OIDC_ISSUER to turn them on." in page
        and "README" not in page
    )
    assert "/access" not in page and 'action="/logout"' not in page

    bob = shared_client.get("/", headers=as_user("bob@example.com", name="Bob Builder")).text
    assert '<span class="me" title="Bob Builder">Bob Builder</span>' in bob and ">Member<" in bob
    assert 'data-copy-identity="bob@example.com"' in bob
    assert 'href="/access/tokens"' in bob and 'href="/access/members"' not in bob
    assert "Settings need" not in bob
    assert '<form method="post" action="/logout" class="signout" aria-label="Sign out">' in bob
    admin = shared_client.get("/").text
    assert ">Admin<" in admin and 'href="/access/members"' in admin


def test_sidebar_collapses_after_eight_projects(client):
    for n in range(10):
        assert push(client, project=f"p{n:02}", id=f"r{n}").status_code == 201
    home = client.get("/").text
    more = home.index('<details class="nav-more">')
    assert home.index('href="/p/p07"') < more < home.index('href="/p/p08"')
    more_label = home.split('<details class="nav-more">')[1].split("</summary>")[0]
    assert (
        '<span class="when-closed">Show 2 more</span><span class="when-open">Show less</span>'
        in more_label
    )
    page = client.get("/p/p09").text
    assert '<details class="nav-more" open>' in page
    assert re.search(r'<a href="/p/p09"[^>]*aria-current="page"', page)


def test_sidebar_links_encode_odd_names(client):
    name = "a b&c#?%"
    conn = db.connect(client.app.state.settings.database_url)
    db.push_report(conn, project=name, title="T", risk="low", body="## A\nx", id="x y")
    conn.close()
    home = client.get("/").text
    assert 'href="/p/a%20b%26c%23%3F%25"' in home
    assert 'href="/r/x%20y"' in client.get("/changes").text
    page = client.get("/p/a%20b%26c%23%3F%25")
    assert page.status_code == 200
    assert re.search(r'<a href="/p/a%20b%26c%23%3F%25"[^>]*aria-current="page"', page.text)
    assert "a b&amp;c#?%" in page.text.split('<nav class="crumbs"')[1].split("</nav>")[0]


def test_brand_is_one_accessible_name_and_logo_is_served(client):
    home = client.get("/").text
    brand = home.split('class="brand"')[1].split("</a>")[0]
    assert '<span class="sr-only">verifair</span><b aria-hidden="true">' in brand
    assert 'src="/static/logo.svg" alt=""' in brand
    assert 'rel="icon" href="/static/logo.svg"' in home
    logo = client.get("/static/logo.svg")
    assert logo.status_code == 200 and logo.headers["content-type"].startswith("image/svg+xml")


def test_name_form_error_takes_its_own_line():
    css = (Path(web.__file__).parent / "static" / "app.css").read_text()
    assert ".me-form .error { flex-basis: 100%; }" in css


def test_page_query_count_does_not_grow_with_reports(client, settings, monkeypatch):
    import psycopg

    connect = db.connect
    execute = psycopg.Cursor.execute
    statements = []

    def traced(cursor, query, *args, **kwargs):
        statements.append(query)
        return execute(cursor, query, *args, **kwargs)

    def seed(start, stop):
        conn = connect(settings.database_url)
        for n in range(start, stop):
            id = f"r{n}"
            db.push_report(
                conn,
                project=f"p{n % 3}",
                title=id,
                risk="low",
                body=SAMPLE,
                id=id,
                author_id="owner000",
                reviewers=["review00"],
            )
            db.sign_version(conn, report_id=id, version=1, signer_id="owner000", signer_name="O")
            db.add_verdict(
                conn,
                report_id=id,
                version=1,
                reviewer_id="other000",
                reviewer_name="X",
                verdict="approve",
            )
        conn.close()

    def counts():
        result = []
        for url in ("/", "/changes", "/changes?scope=reviewer", "/p/p1", "/r/r1"):
            statements.clear()
            assert client.get(url).status_code == 200
            result.append(len(statements))
        return result

    client.cookies.set("vf_id", "review00")
    monkeypatch.setattr(psycopg.Cursor, "execute", traced)
    seed(0, 3)
    few = counts()
    seed(3, 30)
    assert counts() == few


def test_skip_link_and_plain_nav_section_titles(client, shared_client):
    push(client)
    sign(client)
    pages = [client.get(url).text for url in ("/", "/changes", "/p/billing-service", "/r/prorate")]
    pages += [shared_client.get(url).text for url in ("/access/tokens", "/access/members")]
    for page in pages:
        assert '<a class="skip" href="#vf_main">Skip to content</a>' in page
        assert page.count('id="vf_main"') == 1
        assert re.search(r'<main class="[^"]*" id="vf_main"', page)
        assert "<h6" not in page and '<h2 class="nav-h">Your work</h2>' in page


def test_change_row_reads_well_without_the_picture(client):
    push(client, reviewers=["bob00000"], risk="high")
    sign(client)
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve"})
    row = client.get("/changes?tab=all").text.split('<a class="chg"')[1].split("</a>")[0]
    said = re.search(r'<span class="sr-only">([^<]*)</span>', row)[1]
    assert (
        said
        == "Approved, high risk, by agent, reviewers: bob00000 review requested, Alice approved v1"
    )
    outside_stack = re.sub(r'(<span class="stack"[^>]*>).*', r"\1", row, flags=re.S)
    for part in re.findall(r'<span class="(?:si|badge|risk|av|stack)\b[^>]*>', outside_stack):
        assert 'aria-hidden="true"' in part, part


def test_waiting_group_has_a_heading_and_avatars_are_hidden(client):
    push(client)
    client.cookies.set("vf_id", "signer00")
    client.cookies.set("vf_name", "Signer")
    client.post("/r/prorate/sign", data={"version": 1})
    page = client.get("/").text
    summary = page.split('<details class="grp"')[1].split("</summary>")[0]
    assert '<h2 class="grp-t">' in summary and "Waiting on others" in summary
    nav = page.split('class="app-user"')[1]
    assert re.search(r'<span class="av [^"]*" aria-hidden="true">S</span>', nav)


def test_new_token_menu_focuses_the_label(shared_client):
    page = shared_client.get("/access/tokens").text
    menu = page.split('id="vf_new_token_form"')[1].split("</form>")[0]
    assert 'aria-label="Token label" class="inp" autofocus' in menu
    inline = page.split('class="panel inline-form"')[1].split("</form>")[0]
    assert "autofocus" not in inline


def _contrast(a, b):
    def luminance(colour):
        rgb = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        rgb = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]

    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_text_colours_pass_contrast_in_both_themes():
    css = (Path(web.__file__).parent / "static" / "app.css").read_text()
    light = dict(re.findall(r"(--[\w-]+): (#[0-9a-f]{6})", css.split(":root {")[1].split("}")[0]))
    dark_block = css.split(':root[data-theme="dark"] {')[1].split("}")[0]
    dark = light | dict(re.findall(r"(--[\w-]+): (#[0-9a-f]{6})", dark_block))
    for theme in (light, dark):
        warn, bg = theme["--risk-medium"], theme["--bg"]
        tint = "#" + "".join(
            f"{round(int(warn[i : i + 2], 16) * 0.12 + int(bg[i : i + 2], 16) * 0.88):02x}"
            for i in (1, 3, 5)
        )
        for background in (theme["--bg"], theme["--bg-side"]):
            assert _contrast(theme["--text-subtle"], background) >= 4.5
            assert _contrast(theme["--link"], background) >= 4.5
            assert _contrast(theme["--accent"], background) >= 3
        assert _contrast(theme["--warn-text"], tint) >= 4.5
    assert "a { color: var(--link);" in css
    assert ":focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }" in css
    assert ".chg:focus-visible { outline-offset: -2px; background: var(--bg-hover); }" in css
    for rule in (".banner {", ".tag-blocking {", ".side li .n {"):
        assert "color: var(--warn-text)" in css.split(rule)[1].split("}")[0], rule


def test_signer_sees_why_they_cannot_review_their_own_report(client):
    push(client)
    client.cookies.set("vf_id", "signer00")
    client.cookies.set("vf_name", "Signer")
    signed = client.post("/r/prorate/sign", data={"version": 1}).text
    for html in (signed, client.get("/r/prorate").text):
        form = html.split('id="vf_verdict"')[1].split("</form>")[0]
        assert f'<p class="note">{web.SELF_REVIEW}</p>' in form
        assert form.count(" disabled>") == 3
    client.cookies.set("vf_id", "review00")
    form = client.get("/r/prorate").text.split('id="vf_verdict"')[1].split("</form>")[0]
    assert " disabled>" not in form and web.SELF_REVIEW not in form


def test_on_this_page_numbers_sit_in_their_own_column(client):
    push(client)
    rail = client.get("/r/prorate").text.split('id="vf_sidebar"')[1]
    assert '<a href="#intent"><span class="toc-n">1</span>Intent</a>' in rail


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/r/prorate/comments", {"version": 1, "section": "Intent", "body": "Why\x00"}),
        ("/r/prorate/comments", {"version": 1, "section": "Intent\x00", "body": "Why?"}),
        (
            "/r/prorate/comments",
            {"version": 1, "section": "Intent", "body": "Why?", "quote": "Full\x00"},
        ),
        (
            "/r/prorate/comments",
            {"version": 1, "section": "Intent", "body": "Why?", "prefix": "\x00"},
        ),
        (
            "/r/prorate/comments",
            {"version": 1, "section": "Intent", "body": "Why?", "suffix": "\x00"},
        ),
        ("/r/prorate/comments/1/reply", {"version": 1, "body": "\x00"}),
        ("/r/prorate/verdict", {"version": 1, "verdict": "request_changes", "summary": "\x00"}),
    ],
)
def test_web_form_with_a_nul_character_is_422(client, path, data):
    push(client)
    sign(client)
    _open_thread(client)
    r = client.post(path, data=data, follow_redirects=False)
    assert r.status_code == 422
    assert len(api_threads(client)) == 1 and api_threads(client)[0]["replies"] == []
    assert api_report(client)["verdicts"] == []


def test_nul_character_in_a_web_path_is_404(client):
    push(client)
    for path in ("/r/pro%00rate", "/r/pro%00rate/v/1", "/p/billing%00"):
        assert client.get(path).status_code == 404, path


def test_name_cookie_with_a_nul_character_asks_for_a_name(client):
    push(client)
    client.cookies.set("vf_name", "Al%00ice")
    r = client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Hi"})
    assert r.status_code == 200 and 'action="/me"' in r.text
    assert api_threads(client) == []


def test_comment_with_quote_missing_from_section_is_refused(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    data = {"version": 1, "section": "Intent", "quote": "first day", "body": "Where?"}
    r = client.post("/r/prorate/comments", data=data, headers={"HX-Request": "true"})
    assert (r.status_code, r.json()) == (422, {"detail": "quote not found"})
    assert api_threads(client) == []


REQUESTED = re.compile(
    r'<span class="rn" title="([^"]*)">[^<]*</span>(?:<button[^>]*>Remove</button>)?</div>'
    r'\s*<p class="rev-state">([^<]+)</p>'
)


def _rail_reviewers(page):
    return page.split("<h4>Reviewers</h4>")[1].split("<h4>Versions</h4>")[0]


def test_sidebar_skips_the_signer_among_requested_reviewers(client):
    push(client, reviewers=["signer00", "carol000"])
    sign(client)
    rail = _rail_reviewers(client.get("/r/prorate").text)
    assert REQUESTED.findall(rail) == [("carol000", "review requested")]


def test_sidebar_lists_requested_reviewers_next_to_verdicts(client):
    push(client, reviewers=["bob00000", "carol000"])
    sign(client)
    rail = _rail_reviewers(client.get("/r/prorate").text)
    assert "Nobody yet." not in rail
    assert REQUESTED.findall(rail) == [
        ("bob00000", "review requested"),
        ("carol000", "review requested"),
    ]
    client.cookies.set("vf_id", "bob00000")
    client.cookies.set("vf_name", "Bob")
    client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve"})
    rail = _rail_reviewers(client.get("/r/prorate").text)
    assert rail.count('class="rev"') == 2
    assert ">approved v1</span>" in rail and "Bob" in rail
    assert REQUESTED.findall(rail) == [("carol000", "review requested")]


def _plan_review(settings, *, plan="A plain plan without headings.", body=False):
    with closing(db.connect(settings.database_url)) as conn:
        db.push_report(
            conn,
            id="planned",
            project="p",
            title="Planned",
            risk="low",
            body=None,
            body_supplied=False,
            workflow="plan_first",
            plan=plan,
        )
        if body:
            db.sign_version(
                conn,
                report_id="planned",
                version=1,
                target="plan",
                expected_version=1,
                signer_id="owner000",
                signer_name="Owner",
            )
            db.add_verdict(
                conn,
                report_id="planned",
                version=1,
                target="plan",
                reviewer_id="judge000",
                reviewer_name="Judge",
                verdict="approve",
            )
            db.push_report(
                conn,
                id="planned",
                project="p",
                title="Planned",
                risk="low",
                body="## Intent\nImplementation-only evidence.",
                expected_version=1,
            )


def _identity(client, person="owner000", name="Owner"):
    client.cookies.set("vf_id", person)
    client.cookies.set("vf_name", name)


def _target_form(version=1, revision=1, target="plan", **fields):
    return {"version": version, "revision": revision, "target": target} | fields


def test_plan_page_flow_and_history(client, settings):
    _plan_review(settings)
    _identity(client)
    page = client.get("/r/planned")
    assert page.status_code == 200
    assert page.context["target"] == "plan"
    nav = _document_nav(page.text)
    assert '<a href="/r/planned?tab=plan" aria-current="page">Plan</a>' in nav
    assert '<a href="/r/planned?tab=implementation">Implementation</a>' in nav
    assert "Plan revision 1" in page.text and "Sign plan revision 1" in page.text
    general = re.search(r'<form class="cform" id="vf_cform_vf_general"([^>]*)>', page.text)
    assert general and " hidden" not in general[1]
    assert 'aria-label="Comment on plan"' in page.text
    assert page.text.count('id="vf_threads_vf_general"') == 1
    comment = client.post("/r/planned/comments", data=_target_form(body="General plan concern"))
    assert comment.status_code == 200 and "General plan concern" in comment.text
    assert 'id="vf_cform_vf_general"' in comment.text and 'hx-swap-oob="true"' in comment.text
    assert (
        " hidden"
        not in re.search(r'<form class="cform" id="vf_cform_vf_general"([^>]*)>', comment.text)[1]
    )
    with closing(db.connect(settings.database_url)) as conn:
        root = db.list_comments(conn, "planned", target="plan")[0]
        assert root["section"] is None
        db.resolve_thread(conn, "planned", root["id"])
    signed = client.post("/r/planned/sign", data=_target_form())
    assert "Signed by <b>Owner</b>" in signed.text
    _identity(client, "judge000", "Judge")
    approved = client.post("/r/planned/verdict", data=_target_form(verdict="approve"))
    assert "You approved plan revision 1" in approved.text
    assert "Awaiting implementation" in approved.text
    with closing(db.connect(settings.database_url)) as conn:
        db.push_report(
            conn,
            id="planned",
            project="p",
            title="Planned",
            risk="low",
            body="## Intent\nImplementation-only evidence.",
            expected_version=1,
        )
        later = db.add_comment(
            conn,
            report_id="planned",
            version=2,
            author_id="judge000",
            author_name="Judge",
            body="Implementation concern",
        )
    current = client.get("/r/planned")
    assert current.context["target"] == "implementation"
    assert (
        "Implementation-only evidence." in current.text
        and "General plan concern" not in current.text
    )
    plan = client.get("/r/planned?tab=plan")
    assert "General plan concern" in plan.text and "Implementation concern" not in plan.text
    assert "Plan revision 1" in plan.text and "You approved plan revision 1" in plan.text
    assert 'name="version" value="2"' in plan.text and 'name="revision" value="1"' in plan.text
    with closing(db.connect(settings.database_url)) as conn:
        db.push_report(
            conn,
            id="planned",
            project="p",
            title="Planned",
            risk="low",
            body=None,
            body_supplied=False,
            plan="# Revised\nNew direction.",
            expected_version=2,
        )
        db.add_comment(
            conn,
            report_id="planned",
            version=3,
            target="plan",
            author_id="judge000",
            author_name="Judge",
            body="Later plan concern",
        )
        db.add_verdict(
            conn,
            report_id="planned",
            version=3,
            target="plan",
            reviewer_id="judge000",
            reviewer_name="Judge",
            verdict="request_changes",
            summary="Later verdict",
        )
    historical = client.get("/r/planned/v/1?tab=plan")
    assert "A plain plan without headings." in historical.text
    assert "Signed by <b>Owner</b>" in historical.text
    assert "Later plan concern" not in historical.text and "Later verdict" not in historical.text
    assert "hx-post=" not in historical.text
    assert "General plan concern" in historical.text
    implementation = client.get("/r/planned/v/2?tab=implementation")
    assert f'id="vf_c{later["id"]}"' in implementation.text
    assert "New direction." not in implementation.text
    assert 'href="/r/planned?tab=implementation"' in implementation.text
    revised = client.get("/r/planned")
    assert revised.context["target"] == "plan"
    assert "Plan revision 3" in revised.text and "Sign plan revision 3" in revised.text
    assert re.search(
        r'approved <a href="/r/planned/v/1\?tab=plan">plan revision 1</a>', revised.text
    )


@pytest.mark.parametrize("plan", ["Plain paragraph.", "# Heading\n\nH1-only text."])
def test_plan_fallback_sections_and_safe_absent_implementation(client, settings, plan):
    _plan_review(settings, plan=plan)
    _identity(client)
    page = client.get("/r/planned")
    assert page.status_code == 200 and 'data-section="Report"' in page.text
    selected = client.post(
        "/r/planned/comments",
        data=_target_form(
            section="Report",
            quote="Plain" if plan.startswith("Plain") else "H1-only",
            body="Selection concern",
        ),
    )
    assert selected.status_code == 200 and 'data-found="true"' in selected.text
    for alias in ("implementation", "report"):
        empty = client.get(f"/r/planned?tab={alias}")
        assert empty.status_code == 200 and "Implementation has not been attached" in empty.text
        assert "Implementation v1" not in empty.text
        assert 'id="vf_sign"' not in empty.text and 'id="vf_verdict"' not in empty.text
    with closing(db.connect(settings.database_url)) as conn:
        assert len(db.list_comments(conn, "planned", target="plan")) == 1
        assert db.list_comments(conn, "planned") == []


@pytest.mark.parametrize(
    "action,extra",
    [
        ("sign", {}),
        ("comments", {"body": "Do not save"}),
        ("verdict", {"verdict": "approve"}),
    ],
)
@pytest.mark.parametrize("version,revision", [(1, 1), (2, 1)])
def test_plan_web_stale_target(client, settings, action, extra, version, revision):
    _plan_review(settings)
    with closing(db.connect(settings.database_url)) as conn:
        db.push_report(
            conn,
            id="planned",
            project="p",
            title="Planned",
            risk="low",
            body=None,
            body_supplied=False,
            plan="Changed plan.",
            expected_version=1,
        )
    _identity(client)
    result = client.post(f"/r/planned/{action}", data=_target_form(version, revision, **extra))
    assert result.status_code == 200 and 'href="/r/planned?tab=plan"' in result.text
    assert "Reload" in result.text
    with closing(db.connect(settings.database_url)) as conn:
        assert db.get_signature(conn, "planned", 2, target="plan") is None
        assert db.list_comments(conn, "planned", target="plan") == []
        assert db.list_verdicts(conn, "planned", target="plan") == []


def test_plan_navigation_prompts_and_invalid_anchors(client, settings):
    _plan_review(settings, plan="## Shared\nPlan quote.")
    prompt = client.post("/r/planned/sign", data=_target_form())
    assert 'name="next" value="/r/planned?tab=plan"' in prompt.text
    _identity(client)
    for fields in (
        {"section": "Missing"},
        {"section": "Shared", "quote": "wrong"},
        {"quote": "Plan quote."},
    ):
        response = client.post("/r/planned/comments", data=_target_form(body="bad", **fields))
        assert response.status_code == 422
    valid = client.post(
        "/r/planned/comments", data=_target_form(section="Shared", body="Plan note")
    )
    assert valid.status_code == 200
    for field in ('name="target" value="plan"', 'name="revision" value="1"'):
        assert field in valid.text
    with closing(db.connect(settings.database_url)) as conn:
        assert len(db.list_comments(conn, "planned", target="plan")) == 1


def test_plan_gate_thread_partials_refresh_all_controls(client, settings):
    _plan_review(settings, body=True)
    _identity(client, "judge000", "Judge")
    response = client.post(
        "/r/planned/comments", data=_target_form(2, 1, body="New blocking plan concern")
    )
    assert response.status_code == 200
    for id in ("vf_header", "vf_sidebar", "vf_verdict", "vf_nav"):
        assert re.search(rf'id="{id}"[^>]*hx-swap-oob="true"', response.text)
    assert "Changes requested" in response.text
    with closing(db.connect(settings.database_url)) as conn:
        root = db.list_comments(conn, "planned", target="plan")[0]
    _identity(client)
    paused = client.get("/r/planned?tab=implementation")
    assert "current plan" in paused.text.lower() and "Sign implementation" not in paused.text
    refused = client.post("/r/planned/sign", data=_target_form(2, 2, "implementation"))
    assert "current plan" in refused.text.lower()
    resolved = client.post(
        f"/r/planned/comments/{root['id']}/resolve", data={"version": 2, "target": "implementation"}
    )
    assert resolved.status_code == 200 and 'name="target" value="plan"' in resolved.text
    assert re.search(r'id="vf_header"[^>]*hx-swap-oob="true"', resolved.text)
    assert "Sign implementation v2" in client.get("/r/planned?tab=implementation").text
    reopened = client.post(f"/r/planned/comments/{root['id']}/reopen", data={"version": 2})
    assert "Changes requested" in reopened.text
    suggestion = client.post(
        f"/r/planned/comments/{root['id']}/blocking", data={"version": 2, "blocking": "0"}
    )
    assert "Unsigned" in suggestion.text
    with closing(db.connect(settings.database_url)) as conn:
        assert db.thread_root(conn, "planned", root["id"])["target"] == "plan"


def test_plan_comment_validation_runs_under_write_lock(client, settings, monkeypatch):
    _plan_review(settings, plan="## Shared\nOriginal quote.")
    _identity(client)
    actual = db.add_comment
    seen = []

    def add(conn, **kwargs):
        callback = kwargs.get("validate")
        assert callback is not None

        def validate(markdown, revision):
            seen.append(conn.execute("SELECT pg_current_xact_id_if_assigned()").fetchone())
            return callback(markdown, revision)

        return actual(conn, **(kwargs | {"validate": validate}))

    monkeypatch.setattr(db, "add_comment", add)
    response = client.post(
        "/r/planned/comments",
        data=_target_form(section="Shared", quote="Original quote.", body="note"),
    )
    assert response.status_code == 200 and seen


def test_plan_read_assembly_begins_snapshot_before_metadata(client, settings, monkeypatch):
    _plan_review(settings)
    actual = db.get_report
    observed = []

    def read(conn, id):
        observed.append(
            conn.execute("SHOW transaction_isolation").fetchone()["transaction_isolation"]
        )
        return actual(conn, id)

    monkeypatch.setattr(db, "get_report", read)
    assert client.get("/r/planned").status_code == 200
    assert observed and set(observed) == {"repeatable read"}


def test_plan_thread_refresh_keeps_named_user_in_navigation(client, settings):
    _plan_review(settings)
    _identity(client)
    response = client.post("/r/planned/comments", data=_target_form(body="A note"))
    nav = re.search(r'<nav class="app-nav" id="vf_nav".*?</nav>', response.text, re.S)[0]
    assert '<span class="me-name">Owner</span>' in nav


def test_plan_shared_signing_and_moderation_keep_existing_permissions(proxied):
    settings = proxied.app.state.settings
    alice = signed_in("alice@example.com")
    bob = signed_in("bob@example.com")
    with closing(db.connect(settings.database_url)) as conn:
        db.push_report(
            conn,
            id="planned",
            project="p",
            title="Planned",
            risk="low",
            body=None,
            body_supplied=False,
            workflow="plan_first",
            plan="## Shared\nPlan text.",
            author_id="alice@example.com",
        )
    form = _target_form()
    assert proxied.post("/r/planned/sign", data=form, headers=bob).status_code == 403
    assert proxied.post("/r/planned/sign", data=form, headers=alice).status_code == 200
    refused = proxied.post("/r/planned/verdict", data=form | {"verdict": "approve"}, headers=alice)
    assert SELF_COPY in refused.text
    with closing(db.connect(settings.database_url)) as conn:
        root = db.add_comment(
            conn,
            report_id="planned",
            version=1,
            target="plan",
            author_id="bob@example.com",
            author_name="bob",
            body="Bob's blocker",
        )
    denied = proxied.post(
        f"/r/planned/comments/{root['id']}/resolve",
        headers=alice | {"HX-Request": "true"},
        data={"version": 1},
    )
    assert (
        "Only the person who started" in denied.text and 'name="target" value="plan"' in denied.text
    )
    assert (
        proxied.post(
            f"/r/planned/comments/{root['id']}/resolve", headers=bob, data={"version": 1}
        ).status_code
        == 200
    )
    approved = proxied.post("/r/planned/verdict", data=form | {"verdict": "approve"}, headers=bob)
    assert "You approved plan revision 1" in approved.text
    forged = proxied.post(
        f"/r/planned/comments/{root['id']}/reply",
        headers=bob,
        data=form | {"target": "implementation", "body": "forged"},
    )
    assert forged.status_code == 422
    with closing(db.connect(settings.database_url)) as conn:
        assert len(db.list_comments(conn, "planned", target="plan")) == 1
        assert db.list_verdicts(conn, "planned") == []


def test_plan_partial_name_navigation_keeps_selected_document(client, settings):
    _plan_review(settings, body=True)
    _identity(client)
    response = client.post("/r/planned/comments", data=_target_form(2, 1, body="Question"))
    nav = re.search(r'<nav class="app-nav" id="vf_nav".*?</nav>', response.text, re.S)[0]
    assert 'name="next" value="/r/planned?tab=plan"' in nav


@pytest.mark.parametrize(
    "action,helper,fields",
    [
        ("sign", "sign_version", {}),
        ("comments", "add_comment", {"body": "Original concern"}),
        ("verdict", "add_verdict", {"verdict": "approve"}),
    ],
)
def test_plan_push_after_write_requires_reload_before_new_controls(
    client, settings, monkeypatch, action, helper, fields
):
    _plan_review(settings)
    _identity(client)
    if action == "verdict":
        client.post("/r/planned/sign", data=_target_form())
        _identity(client, "judge000", "Judge")
    actual = getattr(db, helper)

    def advance(conn, **kwargs):
        row = actual(conn, **kwargs)
        with closing(db.connect(settings.database_url)) as other:
            db.push_report(
                other,
                id="planned",
                project="p",
                title="Planned",
                risk="low",
                body=None,
                body_supplied=False,
                plan="Unread new plan",
                expected_version=1,
            )
        return row

    monkeypatch.setattr(db, helper, advance)
    response = client.post(f"/r/planned/{action}", data=_target_form(**fields))
    assert response.status_code == 200 and "Reload to review v2" in response.text
    assert 'href="/r/planned?tab=plan"' in response.text
    assert 'name="version" value="2"' not in response.text
    assert 'id="vf_verdict"' not in response.text


def test_plan_implementation_hint_tracks_live_gate_in_partials(client, settings):
    _plan_review(settings)
    _identity(client)
    client.post("/r/planned/sign", data=_target_form())
    _identity(client, "judge000", "Judge")
    ready = client.post("/r/planned/verdict", data=_target_form(verdict="approve"))
    assert "Plan approved · Awaiting implementation" in ready.text
    assert "expected_version: 1" in ready.text
    blocked = client.post("/r/planned/comments", data=_target_form(body="New blocker"))
    assert "Plan approved · Awaiting implementation" not in blocked.text
    with closing(db.connect(settings.database_url)) as conn:
        cid = db.list_comments(conn, "planned", target="plan")[0]["id"]
    restored = client.post(f"/r/planned/comments/{cid}/resolve", data={"version": 1})
    assert "Plan approved · Awaiting implementation" in restored.text


@pytest.mark.parametrize("action", ["resolve", "reopen"])
@pytest.mark.parametrize("displayed_version", [1, None])
def test_moderation_after_push_cannot_expose_unread_approval(
    client, settings, action, displayed_version
):
    _plan_review(settings, plan="Original visible plan")
    _identity(client, "judge000", "Judge")
    with closing(db.connect(settings.database_url)) as conn:
        root = db.add_comment(
            conn,
            report_id="planned",
            version=1,
            target="plan",
            author_id="judge000",
            author_name="Judge",
            body="Existing suggestion",
            blocking=False,
        )
        if action == "reopen":
            db.resolve_thread(conn, "planned", root["id"])
    old = client.get("/r/planned?tab=plan")
    assert "Original visible plan" in old.text
    with closing(db.connect(settings.database_url)) as conn:
        db.push_report(
            conn,
            id="planned",
            project="p",
            title="Planned",
            risk="low",
            body=None,
            body_supplied=False,
            plan="UNREAD REPLACEMENT PLAN",
            expected_version=1,
        )
        db.sign_version(
            conn,
            report_id="planned",
            version=2,
            target="plan",
            expected_version=2,
            signer_id="owner000",
            signer_name="Owner",
        )
    response = client.post(
        f"/r/planned/comments/{root['id']}/{action}",
        data={"version": displayed_version} if displayed_version is not None else {},
    )
    assert response.status_code == 200 and "Reload" in response.text
    assert 'href="/r/planned?tab=plan"' in response.text
    assert (
        'id="vf_verdict"' not in response.text and 'name="version" value="2"' not in response.text
    )
    assert "UNREAD REPLACEMENT PLAN" not in response.text
    with closing(db.connect(settings.database_url)) as conn:
        assert (db.thread_root(conn, "planned", root["id"])["resolved_at"] is not None) == (
            action == "resolve"
        )
        assert db.plan_review_state(conn, "planned")["status"] == "awaiting_review"


@pytest.mark.parametrize("action", ["resolve", "reopen"])
def test_legacy_moderation_without_snapshot_requires_reload(client, settings, action):
    _plan_review(settings)
    _identity(client)
    with closing(db.connect(settings.database_url)) as conn:
        root = db.add_comment(
            conn,
            report_id="planned",
            version=1,
            target="plan",
            author_id="owner000",
            author_name="Owner",
            body="Live concern",
        )
        if action == "reopen":
            db.resolve_thread(conn, "planned", root["id"])
    response = client.post(f"/r/planned/comments/{root['id']}/{action}")
    assert "Reload" in response.text and 'href="/r/planned?tab=plan"' in response.text
    assert 'id="vf_verdict"' not in response.text and 'id="vf_sign"' not in response.text
    with closing(db.connect(settings.database_url)) as conn:
        assert (db.thread_root(conn, "planned", root["id"])["resolved_at"] is not None) == (
            action == "resolve"
        )


@pytest.mark.parametrize(
    "action,context", [("resolve", True), ("resolve", False), ("blocking", True)]
)
def test_moderation_refusal_after_push_cannot_expose_unread_controls(proxied, action, context):
    settings = proxied.app.state.settings
    alice = signed_in("alice@example.com") | {"HX-Request": "true"}
    _plan_review(settings, plan="Original visible plan")
    with closing(db.connect(settings.database_url)) as conn:
        root = db.add_comment(
            conn,
            report_id="planned",
            version=1,
            target="plan",
            author_id="bob@example.com",
            author_name="bob",
            body="Bob's concern",
            blocking=False,
        )
        db.push_report(
            conn,
            id="planned",
            project="p",
            title="Planned",
            risk="low",
            body=None,
            body_supplied=False,
            plan="UNREAD REPLACEMENT PLAN",
            expected_version=1,
        )
        db.sign_version(
            conn,
            report_id="planned",
            version=2,
            target="plan",
            expected_version=2,
            signer_id="bob@example.com",
            signer_name="bob",
        )
    data = {"version": 1} if context else {}
    if action == "blocking":
        data["blocking"] = "1"
    response = proxied.post(f"/r/planned/comments/{root['id']}/{action}", data=data, headers=alice)
    assert response.status_code == 200 and "Reload" in response.text
    assert 'href="/r/planned?tab=plan"' in response.text
    assert (
        'id="vf_verdict"' not in response.text and 'name="version" value="2"' not in response.text
    )
    with closing(db.connect(settings.database_url)) as conn:
        assert db.thread_root(conn, "planned", root["id"])["resolved_at"] is None
        assert db.thread_root(conn, "planned", root["id"])["blocking"] == 0


def test_moderation_buttons_include_displayed_snapshot(client, settings):
    _plan_review(settings)
    _identity(client)
    with closing(db.connect(settings.database_url)) as conn:
        root = db.add_comment(
            conn,
            report_id="planned",
            version=1,
            target="plan",
            author_id="owner000",
            author_name="Owner",
            body="Concern",
        )
        cid = root["id"]
    page = client.get("/r/planned?tab=plan").text
    assert 'name="version" value="1"' in _form_of(page, f"vf_resolve_{cid}")
    with closing(db.connect(settings.database_url)) as conn:
        db.resolve_thread(conn, "planned", cid)
    page = client.get("/r/planned?tab=plan").text
    assert 'name="version" value="1"' in _form_of(page, f"vf_reopen_{cid}")


@pytest.mark.parametrize("workflow", ["artifact", "plan_first"])
@pytest.mark.parametrize("action", ["comments", "verdict"])
def test_unsigned_named_owner_partial_keeps_sign_action(client, settings, workflow, action):
    if workflow == "plan_first":
        _plan_review(settings)
        id, form = "planned", _target_form()
    else:
        push(client)
        id, form = "prorate", {"version": 1, "section": "Intent"}
    _identity(client)
    assert 'id="vf_sign"' in client.get(f"/r/{id}").text
    form |= (
        {"body": "Suggestion", "blocking": "0"} if action == "comments" else {"verdict": "approve"}
    )
    response = client.post(f"/r/{id}/{action}", data=form)
    header = re.search(r'<header[^>]*id="vf_header".*?</header>', response.text, re.S)[0]
    assert 'id="vf_sign"' in header
    if workflow == "plan_first":
        assert "Sign plan revision 1" in header
    else:
        assert "Sign this version" in header


@pytest.mark.parametrize(
    "actor,can_sign", [("alice@example.com", True), ("bob@example.com", False)]
)
@pytest.mark.parametrize("action", ["comments", "verdict"])
def test_shared_unsigned_partial_keeps_sign_permissions(proxied, actor, can_sign, action):
    settings = proxied.app.state.settings
    with closing(db.connect(settings.database_url)) as conn:
        db.push_report(
            conn,
            id="planned",
            project="p",
            title="Planned",
            risk="low",
            body=None,
            body_supplied=False,
            workflow="plan_first",
            plan="Owner's plan",
            author_id="alice@example.com",
        )
    fields = (
        {"body": "Suggestion", "blocking": "0"} if action == "comments" else {"verdict": "approve"}
    )
    response = proxied.post(
        f"/r/planned/{action}",
        headers=signed_in(actor) | {"HX-Request": "true"},
        data=_target_form(**fields),
    )
    header = re.search(r'<header[^>]*id="vf_header".*?</header>', response.text, re.S)[0]
    assert ('id="vf_sign"' in header) == can_sign
    if not can_sign:
        assert "Only alice" in header


@pytest.mark.parametrize(
    "stage,label",
    [
        ("artifact", "billing-service · v1"),
        ("plan", "p · plan revision 1"),
        ("implementation", "p · implementation v2"),
        ("paused", "p · plan revision 1"),
    ],
)
def test_change_row_visible_metadata_names_actionable_document(client, settings, stage, label):
    if stage == "artifact":
        push(client)
    else:
        _plan_review(settings, body=stage in ("implementation", "paused"))
        if stage == "paused":
            with closing(db.connect(settings.database_url)) as conn:
                db.add_comment(
                    conn,
                    report_id="planned",
                    version=1,
                    target="plan",
                    author_id="judge000",
                    author_name="Judge",
                    body="Plan blocker",
                )
    page = client.get("/changes").text
    metadata = re.search(
        r'<div class="chg-m"><span class="chg-ph"[^>]*>.*?</span>(.*?)</div>', page
    )[1]
    assert metadata.startswith(label)


def test_section_comment_link_opens_its_form_without_javascript(client):
    push(client)
    page = client.get("/r/prorate").text
    form_id = re.search(r'<a href="#(vf_cform_[^"]+)" class="cc"', page).group(1)
    assert f'<form class="cform" id="{form_id}" hidden' in page
    assert "html:not(.js) .cform:target { display: block; }" in client.get("/static/app.css").text


def test_unknown_page_in_a_browser_is_a_page_not_json(client):
    for path, message in (
        ("/r/nope", "Report not found."),
        ("/nowhere", "This page does not exist."),
    ):
        r = client.get(path)
        assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
        assert message in r.text and '<a href="/">Go to Inbox</a>' in r.text
    fragment = client.get("/r/nope", headers={"HX-Request": "true"})
    assert fragment.json() == {"detail": "report not found"}
    assert client.get("/api/reports/nope", headers=H).json() == {"detail": "report not found"}


def test_pages_are_not_cached_but_static_files_are(client):
    push(client)
    for path in ("/", "/r/prorate", "/api/reports/prorate", "/missing"):
        assert client.get(path, headers=H).headers["cache-control"] == "no-store", path
    assert "no-store" not in client.get("/static/app.css").headers.get("cache-control", "")


@pytest.mark.parametrize(
    ("base_url", "hsts"),
    [("http://localhost:8000", None), ("https://vf.example.com", "max-age=86400")],
)
def test_hsts_only_when_served_over_https(settings, make_app, base_url, hsts):
    app = make_app(dataclasses.replace(settings, base_url=base_url))
    r = TestClient(app).get("/healthz")
    assert r.headers.get("strict-transport-security") == hsts


SKILL_INSTALL = (
    f"npx skills add romanzdk/verifair@v{importlib.metadata.version('verifair')}"
    " --skill verifair-review -g"
)


def test_first_run_and_tokens_page_show_how_to_install_the_skill(client, shared_client):
    assert SKILL_INSTALL in client.get("/").text
    assert SKILL_INSTALL in shared_client.get("/").text
    assert SKILL_INSTALL in shared_client.get("/access/tokens").text


def test_inbox_asks_members_without_a_token_to_connect_their_agent(shared_client, as_user):
    with closing(db.connect(shared_client.app.state.settings.database_url)) as conn:
        db.push_report(conn, project="p", title="T", risk="low", body="x", id="r", owner_id="o")
    page = shared_client.get("/").text
    assert "Welcome to verifair" not in page and 'id="vf_setup"' in page
    shared_client.post("/access/tokens", data={"label": "laptop", "nonce": "n"})
    assert 'id="vf_setup"' not in shared_client.get("/").text


def test_new_token_fills_in_the_environment_block(shared_client):
    page = shared_client.post("/access/tokens", data={"label": "laptop", "nonce": "n"}).text
    secret = re.search(r'class="codebox">(vf_[^<]+)</textarea>', page)[1]
    assert f"export VERIFAIR_API_TOKEN={secret}</pre>" in page
    assert "export VERIFAIR_API_TOKEN=vf_…</pre>" in shared_client.get("/access/tokens").text


def test_api_explorer_pages_may_load_their_cdn_scripts(client):
    for path in ("/docs", "/redoc"):
        csp = client.get(path).headers["content-security-policy"]
        assert "https://cdn.jsdelivr.net" in csp and "frame-ancestors 'none'" in csp, path
    assert "cdn.jsdelivr.net" not in client.get("/").headers["content-security-policy"]


def test_sidebar_links_to_the_docs_site_and_the_repository(client):
    page = client.get("/").text
    assert 'href="https://romanzdk.github.io/verifair/"' in page
    assert 'href="https://github.com/romanzdk/verifair"' in page


@pytest.mark.parametrize(
    ("body", "html"),
    [
        ("issue: breaks X", '<span class="badge cl-issue">issue</span> breaks X'),
        (
            "Suggestion (non-blocking): try Y",
            '<span class="badge cl-suggestion">suggestion (non-blocking)</span> try Y',
        ),
        ("praise: <b>x</b>", '<span class="badge cl-praise">praise</span> &lt;b&gt;x&lt;/b&gt;'),
        ("todo:", '<span class="badge cl-todo">todo</span> '),
        ("because: nothing", "because: nothing"),
        ("an issue: not at start", "an issue: not at start"),
        (
            "issue (<i>x</i>): y",
            '<span class="badge cl-issue">issue (&lt;i&gt;x&lt;/i&gt;)</span> y',
        ),
    ],
)
def test_comment_label_filter(body, html):
    assert str(web.comment_label(body)) == html


def test_comment_form_has_label_buttons(client):
    push(client)
    page = client.get("/r/prorate").text
    for label in web.COMMENT_LABELS:
        assert f'data-label="{label}"' in page
    assert page.count('data-label="issue" data-blocks') >= 1
    assert 'data-label="note" data-blocks' not in page


def test_labelled_comment_is_stored_unchanged_and_shown_as_badge(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    r = client.post(
        "/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "issue: breaks X"}
    )
    assert '<span class="badge cl-issue">issue</span> breaks X' in r.text
    assert api_threads(client)[0]["body"] == "issue: breaks X"


def test_phone_layout_rules():
    css = (Path(web.__file__).parent / "static" / "app.css").read_text()
    phone = _media(css, "max-width: 640px")
    assert (
        ".secret-row { flex-direction: column;" in phone
        and ".lb, .btn.sm { min-height: 32px; }" in phone
    )
    assert ".content h1 { margin: 0 0 12px; font: 600 20px/28px var(--font); }" in css
    assert ".verdict textarea:disabled { display: none; }" in _media(css, "max-width: 720px")


def test_comment_label_is_fast_on_long_whitespace():
    import time

    started = time.perf_counter()
    for body in ("note" + " " * 20_000 + "x", "note (" + " " * 20_000 + "x", " " * 20_000 + ":"):
        web.comment_label(body)
    assert time.perf_counter() - started < 0.1
    assert str(web.comment_label("issue  (blocking) : y")).startswith(
        '<span class="badge cl-issue">'
    )


def test_api_explorer_pins_its_scripts_with_integrity_hashes(client):
    for path in ("/docs", "/redoc"):
        page = client.get(path).text
        assert re.search(r'src="https://cdn\.jsdelivr\.net/npm/[a-z-]+@\d+\.\d+\.\d+/', page), path
        assert page.count('integrity="sha384-') >= 1 and "@5/" not in page and "@2/" not in page
    assert "fonts.googleapis.com" not in client.get("/redoc").headers["content-security-policy"]
    csp = client.get("/").headers["content-security-policy"]
    assert "base-uri 'none'" in csp and "form-action 'self'" in csp


def test_openapi_reports_the_real_version_and_only_the_api(client):
    spec = client.get("/openapi.json").json()
    assert spec["info"]["version"] == importlib.metadata.version("verifair")
    assert {p for p in spec["paths"] if not p.startswith("/api/")} == {"/healthz", "/readyz"}


def test_footer_links_open_in_a_new_tab(client):
    foot = client.get("/").text.split('<div class="foot">')[1].split("</div>")[0]
    assert foot.count('target="_blank" rel="noopener"') == 2


def test_setup_banner_returns_when_every_token_is_revoked(shared_client):
    with closing(db.connect(shared_client.app.state.settings.database_url)) as conn:
        db.push_report(conn, project="p", title="T", risk="low", body="x", id="r", owner_id="o")
        _, row = db.create_token(conn, owner_id="admin@example.com", label="old")
        assert 'id="vf_setup"' not in shared_client.get("/").text
        db.revoke_token(conn, row["id"])
    assert 'id="vf_setup"' in shared_client.get("/").text
