import asyncio
import json
import threading
import time
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from verifair import db

H = {"Authorization": "Bearer t"}
SAMPLE = "## Intent\nFull new price.\n\n## Edge cases\nFirst day."
NOT_OWNER = "This report belongs to another member."
NO_OWNER = "This report has no owner. Push it again with an agent token under a new id."
SHARED_TOKEN = "The shared token is not accepted in shared mode. Use a member agent token."
AGENT_RESOLVE = "Agent tokens can only resolve threads the agent started on its owner's reports."


def push(client, auth=H, **overrides):
    payload = {"project": "billing-service", "title": "Prorate", "risk": "high", "body": SAMPLE}
    return client.post("/api/reports", json=payload | overrides, headers=auth)


def test_requires_token(client):
    r = client.post("/api/reports", json={})
    assert (r.status_code, r.json()) == (401, {"detail": "invalid token"})
    bad = client.get("/api/reports/x", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


def test_missing_shared_token_never_authenticates(settings, make_app):
    shared = replace(
        settings, api_token=None, identity_header="X-Forwarded-Email", bootstrap_admin_id="boss"
    )
    client = TestClient(make_app(shared))
    for headers, detail in [
        ({"Authorization": "Bearer None"}, SHARED_TOKEN),
        ({"Authorization": "Bearer "}, "invalid token"),
        ({}, "invalid token"),
    ]:
        r = client.get("/api/reports/x", headers=headers)
        assert (r.status_code, r.json()) == (401, {"detail": detail}), headers


def test_push_and_get_report(client):
    r = push(
        client, ticket_url="https://t/BILL-412", links=[{"label": "MR", "url": "https://mr/1"}]
    )
    assert r.status_code == 201
    assert r.json() == {"id": "prorate", "version": 1, "url": "http://localhost:8000/r/prorate"}
    g = client.get("/api/reports/prorate", headers=H).json()
    assert (g["status"], g["current_version"], g["risk"]) == ("unsigned", 1, "high")
    assert g["project"] == "billing-service"
    assert g["ticket_url"] == "https://t/BILL-412"
    assert g["links"] == [{"label": "MR", "url": "https://mr/1"}]
    assert g["author"] == "agent"
    assert [v["version"] for v in g["versions"]] == [1]
    assert g["verdicts"] == []


def test_push_new_version_same_id(client):
    push(client)
    r = push(client, id="prorate", body="## Intent\nChanged.")
    assert r.json()["version"] == 2
    g = client.get("/api/reports/prorate", headers=H).json()
    assert g["current_version"] == 2
    assert [v["version"] for v in g["versions"]] == [1, 2]


def test_push_plan_presence_semantics(client, settings):
    original = "  # Návrh 🦊\n\nOriginal Markdown.\r\n  "
    replacement = "## Different headings\nA revised plan.\n"
    for version, changes in enumerate(
        [{"plan": original}, {}, {"plan": replacement}, {"plan": None}, {}], 1
    ):
        response = push(client, id="prorate", **changes)
        assert response.status_code == 201
        assert response.json() == {
            "id": "prorate",
            "version": version,
            "url": "http://localhost:8000/r/prorate",
        }
    for id, changes in [("omitted", {}), ("null", {"plan": None})]:
        assert push(client, id=id, **changes).status_code == 201
    with closing(db.connect(settings.database_url)) as conn:
        versions = db.list_versions(conn, "prorate")
        assert [v["plan"] for v in versions] == [original, original, replacement, None, None]
        assert [v["body"] for v in versions] == [SAMPLE] * 5
        assert db.get_version(conn, "omitted", 1)["plan"] is None
        assert db.get_version(conn, "null", 1)["plan"] is None
    metadata = client.get("/api/reports/prorate", headers=H).json()
    assert "plan" not in metadata and "body" not in metadata
    assert all(
        set(v) == {"version", "created_at", "commit", "has_implementation", "plan_revision"}
        for v in metadata["versions"]
    )


@pytest.mark.parametrize(
    "plan",
    ["", " \t\n ", [], {}, 42, True, "plan\x00", "plan\ud800", "plan\udfff"],
    ids=["empty", "blank", "list", "object", "number", "boolean", "nul", "high", "low"],
)
def test_push_plan_validation_is_atomic(client, settings, plan):
    assert push(client, id="prorate", plan="# Original").status_code == 201
    with closing(db.connect(settings.database_url)) as conn:
        before_report = db.get_report(conn, "prorate")
        before_versions = db.list_versions(conn, "prorate")
    for id in ["new", "prorate"]:
        payload = {
            "id": id,
            "project": "billing-service",
            "title": "Should not overwrite",
            "risk": "low",
            "body": "changed body",
            "plan": plan,
            "reviewers": ["unexpected-reviewer"],
        }
        response = client.post(
            "/api/reports",
            content=json.dumps(payload),
            headers=H | {"Content-Type": "application/json"},
        )
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["body", "plan"]
    with closing(db.connect(settings.database_url)) as conn:
        assert db.get_report(conn, "new") is None
        assert db.get_report(conn, "prorate") == before_report
        assert db.list_versions(conn, "prorate") == before_versions
        assert db.requested_reviewers(conn, "prorate") == []


def test_plan_does_not_replace_required_body(strict_client, settings):
    valid_body = "## Verification\na\n\n## Rollback\nb\n\n## Security impact\nc"
    assert (
        push(strict_client, body=valid_body, plan="# Arbitrary design\nNo template.").status_code
        == 201
    )
    assert push(strict_client, id="body-only", body=valid_body).status_code == 201
    payload = {"project": "billing-service", "title": "Missing", "risk": "high", "plan": valid_body}
    missing = strict_client.post("/api/reports", json=payload, headers=H)
    assert missing.status_code == 422
    assert missing.json() == {"detail": "artifact reviews require body"}
    for body in [None, "", " \n "]:
        assert push(strict_client, id="prorate", body=body, plan=valid_body).status_code == 422
    invalid = push(strict_client, id="prorate", body="## Other\nx", plan=valid_body)
    assert (invalid.status_code, invalid.json()) == (
        422,
        {"detail": "missing sections", "sections": ["Verification", "Rollback", "Security impact"]},
    )
    with closing(db.connect(settings.database_url)) as conn:
        assert db.get_report(conn, "missing") is None
        assert db.latest_version(conn, "prorate") == 1
        assert db.get_version(conn, "prorate", 1)["body"] == valid_body


@pytest.mark.parametrize("plan", ["# Changed", None], ids=["replace", "remove"])
def test_plan_update_requires_new_report_signature(client, settings, plan):
    assert push(client, plan="# Original").status_code == 201
    client.cookies.set("vf_id", "signer00")
    client.cookies.set("vf_name", "Signer")
    assert client.post("/r/prorate/sign", data={"version": 1}).status_code == 200
    client.cookies.set("vf_id", "alice000")
    client.cookies.set("vf_name", "Alice")
    assert (
        client.post("/r/prorate/verdict", data={"verdict": "approve", "version": 1}).status_code
        == 200
    )
    before = client.get("/api/reports/prorate", headers=H).json()
    assert before["status"] == "approved"
    with closing(db.connect(settings.database_url)) as conn:
        signature = db.get_signature(conn, "prorate", 1)
    response = push(client, id="prorate", plan=plan)
    assert (response.status_code, response.json()["version"]) == (201, 2)
    current = client.get("/api/reports/prorate", headers=H).json()
    assert (current["status"], current["signed_by"], current["signed_at"]) == (
        "unsigned",
        None,
        None,
    )
    assert current["verdicts"] == [before["verdicts"][0] | {"current": False}]
    refused = client.post("/r/prorate/verdict", data={"verdict": "approve", "version": 2})
    assert refused.status_code == 200
    assert "The author has not signed this version yet." in refused.text
    assert client.get("/api/reports/prorate", headers=H).json()["verdicts"] == current["verdicts"]
    with closing(db.connect(settings.database_url)) as conn:
        assert db.get_signature(conn, "prorate", 1) == signature
        assert db.get_signature(conn, "prorate", 2) is None
        assert [v["body"] for v in db.list_versions(conn, "prorate")] == [SAMPLE, SAMPLE]
        assert [v["plan"] for v in db.list_versions(conn, "prorate")] == ["# Original", plan]


def test_plan_headings_and_quotes_are_not_report_comment_anchors(client):
    assert (
        push(client, plan="## Plan only\nUnique plan quote.\n\n## Intent\nPlan intent.").status_code
        == 201
    )
    unknown = _comment(client, section="Plan only", quote="Unique plan quote.")
    assert (unknown.status_code, unknown.json()) == (
        422,
        {"detail": "unknown section", "sections": ["Intent", "Edge cases"]},
    )
    assert _comment(client, section="Intent", quote="Plan intent.").status_code == 422
    assert client.get("/api/reports/prorate/comments", headers=H).json() == {
        "threads": [],
        "current_version": 1,
        "target": "implementation",
        "revision": 1,
    }
    assert _comment(client, section="Intent", quote="Full new price.").status_code == 201


def test_unauthorized_plan_push_preserves_snapshot(
    shared_client, shared_settings, alice, bob, admin
):
    assert push(shared_client, auth=alice, plan="# Original").status_code == 201
    for auth in [bob, admin]:
        response = push(
            shared_client, auth=auth, id="prorate", title="Taken over", plan="# Intruder"
        )
        assert (response.status_code, response.json()) == (403, {"detail": NOT_OWNER})
    with closing(db.connect(shared_settings.database_url)) as conn:
        assert db.latest_version(conn, "prorate") == 1
        assert db.get_version(conn, "prorate", 1)["plan"] == "# Original"
        assert db.get_report(conn, "prorate")["title"] == "Prorate"


def test_push_bad_risk_or_empty_body_422(client):
    assert push(client, risk="extreme").status_code == 422
    assert push(client, body="").status_code == 422
    assert push(client, title="").status_code == 422


def test_push_id_in_other_project_409(client):
    push(client)
    r = push(client, id="prorate", project="other-service")
    assert (r.status_code, r.json()) == (409, {"detail": "id belongs to another project"})


def test_comment_roundtrip_and_open_filter(client):
    push(client)
    root = client.post(
        "/api/reports/prorate/comments",
        json={"body": "Why?", "author": "bot", "section": "Intent", "quote": "Full new price."},
        headers=H,
    )
    assert root.status_code == 201
    cid = root.json()["id"]
    assert root.json() == {
        "id": cid,
        "version": 1,
        "target": "implementation",
        "parent_id": None,
        "section": "Intent",
        "quote": "Full new price.",
        "author": "bot",
        "author_id": "agent",
        "author_type": "agent",
        "body": "Why?",
        "created_at": root.json()["created_at"],
        "blocking": True,
        "resolved": False,
        "resolved_by": None,
        "prefix": None,
        "suffix": None,
    }
    reply = client.post(
        "/api/reports/prorate/comments", json={"body": "Because.", "parent_id": cid}, headers=H
    )
    assert reply.status_code == 201
    assert set(reply.json()) == set(root.json()) and reply.json()["parent_id"] == cid

    threads = client.get("/api/reports/prorate/comments", headers=H).json()["threads"]
    assert len(threads) == 1
    t = threads[0]
    anchor = {"stale": False, "found": True, "moved": False, "match_start": 0}
    assert {k: v for k, v in t.items() if k != "replies"} == root.json() | anchor
    assert t["replies"] == [reply.json() | anchor]

    done = client.post(f"/api/reports/prorate/comments/{cid}/resolve", headers=H)
    assert (done.status_code, done.json()) == (200, {"resolved": True})
    only_open = client.get("/api/reports/prorate/comments?open=true", headers=H).json()
    assert only_open == {
        "threads": [],
        "current_version": 1,
        "target": "implementation",
        "revision": 1,
    }
    everything = client.get("/api/reports/prorate/comments?open=false", headers=H).json()
    assert len(everything["threads"]) == 1
    assert everything["threads"][0]["resolved"] is True
    assert everything["threads"][0]["replies"][0]["resolved"] is True


def test_threads_report_stale_and_found_against_current_version(client):
    push(client)
    for quote in ["Full new price.", None]:
        client.post(
            "/api/reports/prorate/comments",
            json={"body": "Why?", "section": "Intent", "quote": quote},
            headers=H,
        )
    client.post("/api/reports/prorate/comments", json={"body": "Re", "parent_id": 1}, headers=H)
    push(client, id="prorate", body="## Intent\nChanged.")
    quoted, plain = client.get("/api/reports/prorate/comments", headers=H).json()["threads"]
    assert (quoted["stale"], quoted["found"]) == (True, False)
    assert (plain["stale"], plain["found"]) == (True, True)
    [reply] = quoted["replies"]
    assert (reply["stale"], reply["found"]) == (True, False)


def test_unknown_section_422(client):
    push(client)
    r = client.post(
        "/api/reports/prorate/comments", json={"body": "x", "section": "intent"}, headers=H
    )
    assert (r.status_code, r.json()) == (
        422,
        {"detail": "unknown section", "sections": ["Intent", "Edge cases"]},
    )
    push(client, id="prorate", body="## Other\nx")
    r = client.post(
        "/api/reports/prorate/comments", json={"body": "x", "section": "Intent"}, headers=H
    )
    assert r.status_code == 422
    assert client.get("/api/reports/prorate/comments", headers=H).json() == {
        "threads": [],
        "current_version": 2,
        "target": "implementation",
        "revision": 2,
    }


def _comment(client, **body):
    return client.post("/api/reports/prorate/comments", json={"body": "x"} | body, headers=H)


def test_resolve_by_reply_id_twice_and_reply_reopens(client):
    push(client)
    root = _comment(client).json()["id"]
    reply = _comment(client, parent_id=root).json()["id"]
    assert client.post(f"/api/reports/prorate/comments/{reply}/resolve", headers=H).json() == {
        "resolved": True
    }
    [t] = client.get("/api/reports/prorate/comments", headers=H).json()["threads"]
    assert t["resolved"] is True
    again = client.post(f"/api/reports/prorate/comments/{root}/resolve", headers=H)
    assert (again.status_code, again.json()) == (200, {"resolved": True})
    _comment(client, parent_id=root)
    [t] = client.get("/api/reports/prorate/comments?open=true", headers=H).json()["threads"]
    assert t["resolved"] is False and len(t["replies"]) == 2


def test_reply_older_than_root_does_not_break_listing(client, settings):
    from verifair.db import connect

    push(client)
    root = _comment(client).json()["id"]
    reply = _comment(client, parent_id=root).json()["id"]
    conn = connect(settings.database_url)
    conn.execute("UPDATE comments SET created_at='2000-01-01T00:00:00+00:00' WHERE id=%s", (reply,))
    conn.close()
    r = client.get("/api/reports/prorate/comments", headers=H)
    assert r.status_code == 200
    assert [x["id"] for x in r.json()["threads"][0]["replies"]] == [reply]


def test_verdicts_carry_reviewer_id_and_current(client):
    push(client)
    client.cookies.set("vf_id", "signer00")
    client.cookies.set("vf_name", "Signer")
    client.post("/r/prorate/sign", data={"version": 1})
    client.cookies.set("vf_id", "alice000")
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/verdict", data={"verdict": "approve", "version": 1})
    push(client, id="prorate")
    [v] = client.get("/api/reports/prorate", headers=H).json()["verdicts"]
    assert v == {
        "reviewer": "Alice",
        "reviewer_id": "alice000",
        "target": "implementation",
        "verdict": "approve",
        "version": 1,
        "summary": "",
        "created_at": v["created_at"],
        "current": False,
    }


def test_only_latest_verdict_per_reviewer_is_current(client):
    push(client)
    client.cookies.set("vf_id", "signer00")
    client.cookies.set("vf_name", "Signer")
    client.post("/r/prorate/sign", data={"version": 1})
    client.cookies.set("vf_id", "alice000")
    client.cookies.set("vf_name", "Alice")
    client.post(
        "/r/prorate/verdict", data={"verdict": "request_changes", "version": 1, "summary": "no"}
    )
    client.post("/r/prorate/verdict", data={"verdict": "approve", "version": 1})
    g = client.get("/api/reports/prorate", headers=H).json()
    assert g["status"] == "approved"
    assert [(v["verdict"], v["current"]) for v in g["verdicts"]] == [
        ("request_changes", False),
        ("approve", True),
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "   "},
        {"title": "x" * 201},
        {"body": " \n "},
        {"ticket_url": "javascript:alert(1)"},
        {"ticket_url": "ftp://x/y"},
        {"links": [{"label": "x", "url": "javascript:alert(1)"}]},
        {"author": "x" * 61},
        {"unexpected": 1},
        {"links": [{"label": "x", "url": "https://x", "extra": 1}]},
    ],
)
def test_push_validation_422(client, overrides):
    assert push(client, **overrides).status_code == 422


def test_push_strips_title(client):
    push(client, title="  Prorate  ")
    assert client.get("/api/reports/prorate", headers=H).json()["title"] == "Prorate"


@pytest.mark.parametrize(
    "body",
    [
        {"body": "  "},
        {"body": "x" * 20001},
        {"body": "x", "quote": "q" * 2001},
        {"body": "x", "section": "s" * 2001},
        {"body": "x", "author": "a" * 61},
        {"body": "x", "unexpected": 1},
    ],
)
def test_comment_validation_422(client, body):
    push(client)
    r = client.post("/api/reports/prorate/comments", json=body, headers=H)
    assert r.status_code == 422


def test_new_root_comment_goes_on_current_version(client):
    push(client)
    push(client, id="prorate")
    c = client.post("/api/reports/prorate/comments", json={"body": "Hi"}, headers=H)
    assert c.json()["version"] == 2


def test_unknown_report_404(client):
    missing = {"detail": "report not found"}
    r = client.get("/api/reports/nope", headers=H)
    assert (r.status_code, r.json()) == (404, missing)
    assert client.get("/api/reports/nope/comments", headers=H).status_code == 404
    assert (
        client.post("/api/reports/nope/comments", json={"body": "x"}, headers=H).status_code == 404
    )
    assert client.post("/api/reports/nope/comments/1/resolve", headers=H).status_code == 404


def test_unknown_parent_and_comment_404(client):
    push(client)
    r = client.post("/api/reports/prorate/comments", json={"body": "x", "parent_id": 99}, headers=H)
    assert (r.status_code, r.json()) == (404, {"detail": "parent not found"})
    assert client.post("/api/reports/prorate/comments/99/resolve", headers=H).status_code == 404


def test_push_and_comment_notify_when_slack_configured(settings, make_app, monkeypatch):
    from verifair import notify

    sent = []
    monkeypatch.setattr(notify, "send", lambda settings, text: sent.append(text))
    settings = replace(settings, slack_webhook_url="https://hooks.example/x")
    client = TestClient(make_app(settings))
    push(client)
    assert len(sent) == 1
    assert sent[0].startswith("Draft report v1 in billing-service")
    client.post("/api/reports/prorate/comments", json={"body": "Why?", "author": "bot"}, headers=H)
    assert len(sent) == 2
    assert sent[1].startswith("bot commented on Prorate › report: Why?")


@pytest.mark.parametrize(
    "overrides",
    [{"id": "feature/foo"}, {"id": ""}, {"id": ".."}, {"project": ""}, {"project": "  "}],
)
def test_push_rejects_ids_and_projects_that_are_not_url_safe(client, overrides):
    assert push(client, **overrides).status_code == 422


def test_push_accepts_dotted_project_and_id(client):
    r = push(client, project="my-service.v2", id="my-service.v2_fix")
    assert r.status_code == 201
    assert client.get("/r/my-service.v2_fix").status_code == 200
    assert client.get("/p/my-service.v2").status_code == 200


@pytest.mark.parametrize(
    "anchor", [{"section": "Intent"}, {"quote": "Full"}, {"prefix": "x"}, {"suffix": "x"}]
)
def test_reply_with_anchor_field_422(client, anchor):
    push(client)
    root = _comment(client).json()["id"]
    r = _comment(client, parent_id=root, **anchor)
    assert (r.status_code, r.json()) == (
        422,
        {"detail": "replies cannot set section, quote, prefix or suffix"},
    )
    assert (
        client.get("/api/reports/prorate/comments", headers=H).json()["threads"][0]["replies"] == []
    )


def test_quote_without_section_422(client):
    push(client)
    r = _comment(client, quote="Full new price.")
    assert (r.status_code, r.json()) == (422, {"detail": "quote requires section"})
    assert client.get("/api/reports/prorate/comments", headers=H).json() == {
        "threads": [],
        "current_version": 1,
        "target": "implementation",
        "revision": 1,
    }


def test_blank_title_says_must_not_be_blank(client):
    r = push(client, title="   ")
    assert r.status_code == 422
    assert "must not be blank" in r.json()["detail"][0]["msg"]


@pytest.fixture
def strict_client(settings, make_app):
    required = ("Verification", "Rollback", "Security impact")
    return TestClient(make_app(replace(settings, required_sections=required)))


def test_push_missing_required_sections_422(strict_client):
    body = "## Verification\nran tests\n\n## rollback\nlower case does not count"
    r = push(strict_client, body=body)
    assert r.status_code == 422
    assert r.json() == {"detail": "missing sections", "sections": ["Rollback", "Security impact"]}
    assert strict_client.get("/api/reports/prorate", headers=H).status_code == 404


def test_push_with_all_required_sections_ok(strict_client):
    body = "## Verification\na\n\n## Rollback\nb\n\n## Security impact\nc"
    assert push(strict_client, body=body).status_code == 201


def test_required_heading_names_follow_markdown_closing_hashes(strict_client):
    body = "  ## Verification ###\na\n\n## Rollback #####\nb\n\n## Security impact #\nc"
    response = push(strict_client, body=body)
    assert response.status_code == 201
    page = strict_client.get("/r/prorate").text
    assert 'data-section="Verification"' in page
    assert 'data-section="Rollback"' in page
    assert 'data-section="Security impact"' in page


@pytest.mark.parametrize("fence", ["  ```", "   ~~~~", "```markdown"])
def test_required_headings_inside_markdown_fences_do_not_count(strict_client, fence):
    closer = "~~~" if "~" in fence else "```"
    body = f"{fence}\n## Verification\na\n## Rollback\nb\n## Security impact\nc\n{closer}"
    response = push(strict_client, body=body)
    assert response.status_code == 422
    assert response.json()["sections"] == ["Verification", "Rollback", "Security impact"]


@pytest.mark.parametrize("required", ["Intro", "Report"])
def test_synthetic_ui_sections_do_not_satisfy_required_headings(settings, make_app, required):
    client = TestClient(make_app(replace(settings, required_sections=(required,))))
    body = "preamble\n\n## Other\ntext" if required == "Intro" else "just text"
    response = push(client, body=body)
    assert response.status_code == 422
    assert response.json()["sections"] == [required]
    assert push(client, body=f"## {required}\nreal heading").status_code == 201


@pytest.mark.parametrize("initial", [True, False])
def test_existing_replies_inherit_live_root_type(client, settings, initial):
    from contextlib import closing

    push(client)
    root = _comment(client, blocking=initial).json()
    reply = _comment(client, parent_id=root["id"]).json()
    nested = _comment(client, parent_id=reply["id"]).json()
    with closing(db.connect(settings.database_url)) as conn:
        db.set_blocking(conn, "prorate", root["id"], not initial)
    [thread] = client.get("/api/reports/prorate/comments", headers=H).json()["threads"]
    assert thread["blocking"] is not initial
    assert [r["id"] for r in thread["replies"]] == [reply["id"], nested["id"]]
    assert all(r["blocking"] is not initial for r in thread["replies"])
    later = _comment(client, parent_id=nested["id"]).json()
    assert later["blocking"] is not initial


def test_empty_required_sections_disables_the_check(client):
    assert push(client, body="no headings at all").status_code == 201


def test_load_settings_required_sections(monkeypatch):
    from verifair.config import load_settings

    monkeypatch.setenv("VERIFAIR_API_TOKEN", "t")
    monkeypatch.delenv("VERIFAIR_REQUIRED_SECTIONS", raising=False)
    monkeypatch.delenv("VERIFAIR_IDENTITY_HEADER", raising=False)
    s = load_settings()
    assert s.required_sections == ("Verification", "Rollback", "Security impact")
    assert s.identity_header is None
    monkeypatch.setenv("VERIFAIR_REQUIRED_SECTIONS", " A , ,B ")
    monkeypatch.setenv("VERIFAIR_IDENTITY_HEADER", "X-User")
    s = load_settings()
    assert (s.required_sections, s.identity_header) == (("A", "B"), "X-User")
    monkeypatch.setenv("VERIFAIR_REQUIRED_SECTIONS", "")
    monkeypatch.setenv("VERIFAIR_IDENTITY_HEADER", "")
    s = load_settings()
    assert (s.required_sections, s.identity_header) == ((), None)


def test_provenance_and_commit_roundtrip(client):
    r = push(
        client,
        agent="claude",
        model="opus",
        session_url="https://sessions.example/1",
        commit="a1b2c3d",
    )
    assert r.status_code == 201
    push(
        client,
        id="prorate",
        body="## Intent\nv2",
        commit="a" * 40,
        agent="claude",
        model="opus",
        session_url="https://sessions.example/1",
    )
    g = client.get("/api/reports/prorate", headers=H).json()
    assert (g["agent"], g["model"], g["session_url"]) == (
        "claude",
        "opus",
        "https://sessions.example/1",
    )
    assert (g["signed_by"], g["signed_at"]) == (None, None)
    assert [v["commit"] for v in g["versions"]] == ["a1b2c3d", "a" * 40]


def test_provenance_defaults_to_null(client):
    push(client)
    g = client.get("/api/reports/prorate", headers=H).json()
    assert (g["agent"], g["model"], g["session_url"]) == (None, None, None)
    assert [v["commit"] for v in g["versions"]] == [None]


@pytest.mark.parametrize(
    "overrides",
    [
        {"commit": "ABCDEF1"},
        {"commit": "abc12"},
        {"commit": "g" * 7},
        {"commit": "a" * 41},
        {"agent": "x" * 61},
        {"model": "x" * 61},
        {"session_url": "javascript:alert(1)"},
    ],
)
def test_push_rejects_bad_provenance(client, overrides):
    assert push(client, **overrides).status_code == 422


def test_signed_report_shows_signer_and_status(client, settings):
    from verifair import db

    push(client)
    with closing(db.connect(settings.database_url)) as conn:
        db.sign_version(conn, report_id="prorate", version=1, signer_id="u1", signer_name="Alice")
    g = client.get("/api/reports/prorate", headers=H).json()
    assert (g["status"], g["signed_by"]) == ("awaiting_review", "Alice")
    assert g["signed_at"]


def test_comment_blocking_roundtrip(client):
    push(client)
    url = "/api/reports/prorate/comments"
    default = client.post(url, json={"body": "a"}, headers=H).json()
    assert default["blocking"] is True
    nit = client.post(url, json={"body": "b", "blocking": False}, headers=H).json()
    assert nit["blocking"] is False
    reply = client.post(url, json={"body": "c", "parent_id": nit["id"]}, headers=H).json()
    assert reply["blocking"] is False
    threads = client.get(url, headers=H).json()["threads"]
    assert [t["blocking"] for t in threads] == [True, False]


DUP = "## Intent\nthe fee is 5. later the fee is 6.\n\n## Edge cases\nFirst day."


def test_ambiguous_quote_without_context_422(client):
    push(client, body=DUP)
    r = _comment(client, section="Intent", quote="the fee is")
    assert (r.status_code, r.json()) == (422, {"detail": "ambiguous quote"})
    assert client.get("/api/reports/prorate/comments", headers=H).json() == {
        "threads": [],
        "current_version": 1,
        "target": "implementation",
        "revision": 1,
    }
    assert _comment(client, section="Edge cases", quote="First day").status_code == 201


@pytest.mark.parametrize("quote", ["not in the text", "First day"])
def test_quote_missing_from_section_422(client, quote):
    push(client)
    r = _comment(client, section="Intent", quote=quote)
    assert (r.status_code, r.json()) == (422, {"detail": "quote not found"})
    assert client.get("/api/reports/prorate/comments", headers=H).json() == {
        "threads": [],
        "current_version": 1,
        "target": "implementation",
        "revision": 1,
    }
    assert _comment(client, section="Intent", quote="Full  new\nprice").status_code == 201


def test_prefix_suffix_pick_occurrence_and_roundtrip(client):
    push(client, body=DUP)
    r = _comment(client, section="Intent", quote="the fee is", prefix="5. later ", suffix=" 6.")
    assert r.status_code == 201
    assert (r.json()["prefix"], r.json()["suffix"]) == ("5. later ", " 6.")
    [t] = client.get("/api/reports/prorate/comments", headers=H).json()["threads"]
    assert (t["found"], t["moved"], t["match_start"]) == (True, False, 20)
    assert _comment(client, section="Intent", quote="x", prefix="p" * 65).status_code == 422


def test_thread_moved_after_heading_rename(client):
    push(client)
    _comment(client, section="Intent", quote="Full new price.")
    push(client, id="prorate", body="## Goal\nFull new price.\n\n## Edge cases\nFirst day.")
    [t] = client.get("/api/reports/prorate/comments", headers=H).json()["threads"]
    assert (t["found"], t["moved"], t["match_start"]) == (True, True, 0)


def _wait(client, query="", **kw):
    return client.get(f"/api/reports/prorate/wait?{query}", headers=H, **kw)


def _later(settings, delay, action):
    def run():
        time.sleep(delay)
        conn = db.connect(settings.database_url)
        try:
            action(conn)
        finally:
            conn.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def _verdict(conn):
    db.add_verdict(
        conn,
        report_id="prorate",
        version=1,
        reviewer_id="bob00000",
        reviewer_name="Bob",
        verdict="request_changes",
        summary="No.",
    )


def _root_comment(conn):
    db.add_comment(
        conn,
        report_id="prorate",
        version=1,
        author_id="bob00000",
        author_name="Bob",
        body="Why?",
    )


def test_wait_returns_verdict_inserted_later(client, settings):
    push(client)
    thread = _later(settings, 0.6, _verdict)
    start = time.monotonic()
    r = _wait(client, "for=verdict&timeout=10")
    thread.join()
    assert time.monotonic() - start < 3
    body = r.json()
    assert (r.status_code, body["event"]) == (200, "verdict")
    assert body["verdict"]["reviewer"] == "Bob"
    assert body["verdict"]["verdict"] == "request_changes"
    assert body["verdict"]["current"] is True
    assert body["next_since"] == body["verdict"]["created_at"]


def test_wait_returns_new_root_comment_but_not_reply(client, settings):
    push(client)
    root = _comment(client).json()
    thread = _later(
        settings,
        0.6,
        lambda c: db.add_comment(
            c,
            report_id="prorate",
            version=1,
            author_id="bob00000",
            author_name="Bob",
            body="reply",
            parent_id=root["id"],
        ),
    )
    r = _wait(client, params={"since": root["created_at"], "for": "comment", "timeout": 1})
    thread.join()
    assert r.json()["event"] == "timeout"
    thread = _later(settings, 0.6, _root_comment)
    r = _wait(client, "for=comment&timeout=10")
    thread.join()
    body = r.json()
    assert (body["event"], body["comment"]["body"], body["comment"]["author_type"]) == (
        "comment",
        "Why?",
        "human",
    )
    assert body["next_since"] == body["comment"]["created_at"]


def test_wait_for_filters_event_kind(client, settings):
    push(client)
    thread = _later(settings, 0.3, _verdict)
    r = _wait(client, "for=comment&timeout=1")
    thread.join()
    assert r.json()["event"] == "timeout"


def test_wait_any_returns_existing_item_after_since(client):
    push(client)
    posted = _comment(client).json()
    r = _wait(client, "since=2000-01-01T00:00:00%2B00:00&timeout=1")
    assert r.json()["event"] == "comment"
    assert r.json()["comment"] == posted


def test_wait_since_filters_out_older_items(client):
    push(client)
    _comment(client)
    r = _wait(client, "since=2999-01-01T00:00:00%2B00:00&timeout=1")
    assert r.json()["event"] == "timeout"


def test_wait_ignores_items_that_exist_when_it_starts(client):
    push(client)
    _comment(client)
    r = _wait(client, "timeout=1")
    assert r.json()["event"] == "timeout"


def test_wait_timeout_returns_since(client):
    push(client)
    start = time.monotonic()
    r = _wait(client, "timeout=1&since=2999-01-01T00:00:00%2B00:00")
    assert time.monotonic() - start < 1.8
    assert (r.status_code, r.json()) == (
        200,
        {
            "event": "timeout",
            "current_version": 1,
            "target": "implementation",
            "revision": 1,
            "since": "2999-01-01T00:00:00+00:00",
            "next_since": "2999-01-01T00:00:00+00:00",
        },
    )


@pytest.mark.parametrize(
    "query", ["for=nothing", "timeout=0", "timeout=56", "timeout=x", "since=yesterday"]
)
def test_wait_invalid_query_422(client, query):
    push(client)
    assert _wait(client, query).status_code == 422


def test_wait_unknown_report_404(client):
    r = client.get("/api/reports/nope/wait?timeout=1", headers=H)
    assert r.status_code == 404


def test_wait_requires_token(client):
    push(client)
    assert client.get("/api/reports/prorate/wait?timeout=1").status_code == 401


@pytest.mark.parametrize("kind", ["verdict", "comment"])
def test_wait_ignores_events_on_old_versions(client, settings, kind):
    push(client)
    conn = db.connect(settings.database_url)
    try:
        (_verdict if kind == "verdict" else _root_comment)(conn)
    finally:
        conn.close()
    push(client, id="prorate")
    assert (
        _wait(client, f"for={kind}&since=2000-01-01T00:00:00Z&timeout=1").json()["event"]
        == "timeout"
    )


def test_wait_tracks_new_current_version(client, settings):
    push(client)

    def update(conn):
        db.push_report(
            conn, id="prorate", project="billing-service", title="Updated", risk="high", body=SAMPLE
        )
        conn.execute(
            "INSERT INTO verdicts (report_id, version, reviewer_id, reviewer_name, verdict,"
            " summary, created_at, target) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                "prorate",
                1,
                "bob00000",
                "Bob",
                "request_changes",
                "Late historical feedback",
                db._now(),
                "implementation",
            ),
        )
        db.add_comment(
            conn,
            report_id="prorate",
            version=2,
            author_id="bob",
            author_name="Bob",
            body="New version",
        )

    thread = _later(settings, 0.2, update)
    response = _wait(client, "timeout=3")
    thread.join()
    assert response.json()["event"] == "comment"
    assert response.json()["comment"]["version"] == 2


def test_wait_does_not_starve_writer_requests(client):
    push(client)

    async def run():
        transport = httpx.ASGITransport(app=client.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            waiters = [
                asyncio.create_task(http.get("/api/reports/prorate/wait?timeout=3", headers=H))
                for _ in range(45)
            ]
            await asyncio.sleep(0.1)
            start = time.monotonic()
            written = await http.post(
                "/api/reports/prorate/comments", headers=H, json={"body": "Concurrent"}
            )
            elapsed = time.monotonic() - start
            responses = await asyncio.gather(*waiters)
            assert written.status_code == 201
            assert elapsed < 1.5
            assert all(r.json()["event"] == "comment" for r in responses)

    asyncio.run(run())


def test_wait_since_compares_instants_and_excludes_equal_timestamp(client):
    push(client)
    root = _comment(client).json()
    same_instant = (
        datetime.fromisoformat(root["created_at"])
        .astimezone(timezone(timedelta(hours=-5)))
        .isoformat()
    )
    response = _wait(client, params={"since": same_instant, "timeout": 1})
    assert response.json()["event"] == "timeout"


def test_wait_without_since_detects_new_rows_in_the_same_second(client, settings, monkeypatch):
    monkeypatch.setattr(db, "_now", lambda: "2000-01-01T00:00:00+00:00")
    push(client)
    _comment(client)
    thread = _later(settings, 0.2, _root_comment)
    response = _wait(client, "timeout=2")
    thread.join()
    assert response.json()["comment"]["body"] == "Why?"
    assert response.json()["next_since"] == "2000-01-01T00:00:00+00:00"


def test_local_mode_refuses_an_empty_shared_token(settings, make_app):
    client = TestClient(make_app(replace(settings, api_token="")))
    for headers in [{"Authorization": "Bearer "}, {"Authorization": "Bearer"}, {}]:
        r = client.get("/api/reports/x", headers=headers)
        assert (r.status_code, r.json()) == (401, {"detail": "invalid token"}), headers


def _agent(settings, id, role="member"):
    with closing(db.connect(settings.database_url)) as conn:
        if db.get_user(conn, id) is None:
            db.add_user(conn, id=id, name=id.partition("@")[0], role=role)
        token, _ = db.create_token(conn, owner_id=id, label="agent")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def alice(shared_client, shared_settings):
    return _agent(shared_settings, "alice@example.com")


@pytest.fixture
def bob(shared_client, shared_settings):
    return _agent(shared_settings, "bob@example.com")


@pytest.fixture
def admin(shared_client, shared_settings):
    return _agent(shared_settings, "admin@example.com")


def test_shared_mode_refuses_the_shared_token_and_accepts_member_tokens(shared_settings, make_app):
    client = TestClient(make_app(replace(shared_settings, api_token="t")))
    alice = _agent(shared_settings, "alice@example.com")
    for auth in (H, {"Authorization": "Bearer change-me"}):
        r = push(client, auth=auth)
        assert (r.status_code, r.json()) == (401, {"detail": SHARED_TOKEN})
    assert push(client, auth=alice).status_code == 201


def test_member_token_push_sets_owner_and_every_member_reads(shared_client, alice, bob):
    assert push(shared_client, auth=alice, author="Someone else").status_code == 201
    report = shared_client.get("/api/reports/prorate", headers=bob).json()
    assert (report["author"], report["author_id"]) == ("alice", "alice@example.com")
    assert shared_client.get("/api/reports/prorate/comments", headers=bob).json() == {
        "threads": [],
        "current_version": 1,
        "target": "implementation",
        "revision": 1,
    }
    waited = shared_client.get("/api/reports/prorate/wait?timeout=0.1", headers=bob)
    assert waited.json()["event"] == "timeout"
    again = push(shared_client, auth=alice, id="prorate", author_id="alice@example.com")
    assert again.json()["version"] == 2


def test_revoked_or_deactivated_tokens_are_refused_on_the_next_call(
    shared_client, shared_settings, alice, bob
):
    assert push(shared_client, auth=alice).status_code == 201
    assert shared_client.get("/api/reports/prorate", headers=bob).status_code == 200
    with closing(db.connect(shared_settings.database_url)) as conn:
        [token] = db.list_tokens(conn, "alice@example.com")
        db.revoke_token(conn, token["id"])
        db.update_user(conn, "bob@example.com", active=False)
    for auth in (alice, bob):
        r = shared_client.get("/api/reports/prorate", headers=auth)
        assert (r.status_code, r.json()) == (401, {"detail": "invalid token"})


@pytest.mark.parametrize(
    "shape",
    ["", "Bearer", "Bearer ", "bearer {t}", "Token {t}", "{t}", "Bearer  {t}", "Bearer {t}x"],
)
def test_shared_mode_malformed_bearer_is_401(shared_client, alice, shape):
    token = alice["Authorization"].removeprefix("Bearer ")
    r = shared_client.get("/api/reports/x", headers={"Authorization": shape.format(t=token)})
    assert (r.status_code, r.json()) == (401, {"detail": "invalid token"})


def test_author_id_must_be_the_token_owner(shared_client, alice, bob):
    r = push(shared_client, auth=alice, author_id="bob@example.com")
    assert (r.status_code, r.json()) == (403, {"detail": "author_id must be the token owner."})
    assert shared_client.get("/api/reports/prorate", headers=alice).status_code == 404


def test_push_to_another_members_report_is_refused_even_for_admins(
    shared_client, shared_settings, alice, bob, admin
):
    assert push(shared_client, auth=alice).status_code == 201
    with closing(db.connect(shared_settings.database_url)) as conn:
        db.push_report(conn, project="billing-service", title="Legacy", risk="low", body=SAMPLE)
    for auth, id, detail in [
        (bob, "prorate", NOT_OWNER),
        (admin, "prorate", NOT_OWNER),
        (alice, "legacy", NO_OWNER),
    ]:
        r = push(shared_client, auth=auth, id=id, title="Taken over")
        assert (r.status_code, r.json()) == (403, {"detail": detail}), id
    for id, title, owner in [
        ("prorate", "Prorate", "alice@example.com"),
        ("legacy", "Legacy", None),
    ]:
        report = shared_client.get(f"/api/reports/{id}", headers=alice).json()
        assert (report["title"], report["current_version"], report["author_id"]) == (
            title,
            1,
            owner,
        )


def test_shared_mode_reviewers_must_be_active_members(shared_client, shared_settings, alice, bob):
    with closing(db.connect(shared_settings.database_url)) as conn:
        db.add_user(conn, id="gone@example.com", name="gone", role="member")
        db.update_user(conn, "gone@example.com", active=False)
    for reviewers, bad in [
        (
            ["bob@example.com", "ghost@example.com", "zed@example.com", "Ghost@example.com"],
            ["ghost@example.com", "zed@example.com"],
        ),
        (["gone@example.com"], ["gone@example.com"]),
    ]:
        r = push(shared_client, auth=alice, reviewers=reviewers)
        assert (r.status_code, r.json()) == (
            422,
            {"detail": "inactive reviewers", "reviewers": bad},
        )
    assert shared_client.get("/api/reports/prorate", headers=alice).status_code == 404
    ok = push(shared_client, auth=alice, reviewers=["bob@example.com", "admin@example.com"])
    assert ok.status_code == 201
    report = shared_client.get("/api/reports/prorate", headers=alice).json()
    assert report["reviewers"] == ["admin@example.com", "bob@example.com"]


def test_agents_comment_only_on_their_owners_reports(shared_client, alice, bob, admin):
    push(shared_client, auth=alice)
    url = "/api/reports/prorate/comments"
    for auth in (bob, admin):
        r = shared_client.post(url, json={"body": "Hi"}, headers=auth)
        assert (r.status_code, r.json()) == (403, {"detail": NOT_OWNER})
    ok = shared_client.post(url, json={"body": "Hi", "author": "Claude"}, headers=alice)
    assert ok.status_code == 201
    assert (ok.json()["author_id"], ok.json()["author"]) == ("agent", "alice's agent")
    reply = shared_client.post(
        url, json={"body": "Re", "parent_id": ok.json()["id"]}, headers=alice
    )
    assert reply.json()["author"] == "alice's agent"
    assert len(shared_client.get(url, headers=bob).json()["threads"]) == 1


def test_agents_resolve_only_agent_threads_on_their_owners_reports(
    shared_client, shared_settings, alice, bob, admin
):
    push(shared_client, auth=alice)
    url = "/api/reports/prorate/comments"
    agent_thread = shared_client.post(url, json={"body": "Note"}, headers=alice).json()["id"]
    with closing(db.connect(shared_settings.database_url)) as conn:
        human_thread, owners_thread = (
            db.add_comment(
                conn, report_id="prorate", version=1, author_id=who, author_name=who, body="Why?"
            )["id"]
            for who in ("bob@example.com", "alice@example.com")
        )

    def resolve(cid, auth):
        return shared_client.post(f"{url}/{cid}/resolve", headers=auth)

    for cid, auth, detail in [
        (human_thread, alice, AGENT_RESOLVE),
        (human_thread, bob, NOT_OWNER),
        (human_thread, admin, NOT_OWNER),
        (owners_thread, alice, AGENT_RESOLVE),
        (agent_thread, bob, NOT_OWNER),
        (agent_thread, admin, NOT_OWNER),
    ]:
        r = resolve(cid, auth)
        assert (r.status_code, r.json()) == (403, {"detail": detail}), cid
    assert resolve(999, alice).status_code == 404
    reply = shared_client.post(
        url, json={"body": "Done", "parent_id": agent_thread}, headers=alice
    ).json()["id"]
    assert resolve(reply, alice).json() == {"resolved": True}
    with closing(db.connect(shared_settings.database_url)) as conn:
        rows = {c["id"]: c for c in db.list_comments(conn, "prorate")}
    assert rows[agent_thread]["resolved_by"] == "agent"
    assert rows[human_thread]["resolved_at"] is rows[owners_thread]["resolved_at"] is None


def test_agents_cannot_comment_on_or_resolve_threads_of_unowned_reports(
    shared_client, shared_settings, alice
):
    with closing(db.connect(shared_settings.database_url)) as conn:
        db.push_report(conn, project="p", title="Legacy", risk="low", body=SAMPLE)
        cid = db.add_comment(
            conn, report_id="legacy", version=1, author_id="agent", author_name="a", body="x"
        )["id"]
    url = "/api/reports/legacy/comments"
    for r in [
        shared_client.post(url, json={"body": "Hi"}, headers=alice),
        shared_client.post(url, json={"body": "Hi", "parent_id": cid}, headers=alice),
        shared_client.post(f"{url}/{cid}/resolve", headers=alice),
    ]:
        assert (r.status_code, r.json()) == (403, {"detail": NO_OWNER})
    [thread] = shared_client.get(url, headers=alice).json()["threads"]
    assert (thread["resolved"], thread["replies"]) == (False, [])


def test_shared_mode_push_lower_cases_author_id_and_reviewers(shared_client, alice, bob):
    r = push(
        shared_client,
        auth=alice,
        author_id="Alice@Example.com",
        reviewers=["Bob@Example.com", "BOB@example.com"],
    )
    assert r.status_code == 201
    report = shared_client.get("/api/reports/prorate", headers=alice).json()
    assert (report["author_id"], report["reviewers"]) == ("alice@example.com", ["bob@example.com"])


def test_local_mode_push_keeps_the_case_of_ids(client):
    assert push(client, author_id="Alice000", reviewers=["Bob00000"]).status_code == 201
    report = client.get("/api/reports/prorate", headers=H).json()
    assert (report["author_id"], report["reviewers"]) == ("Alice000", ["Bob00000"])


def test_shared_mode_wait_stops_when_the_token_is_revoked(shared_client, shared_settings, alice):
    push(shared_client, auth=alice)

    def revoke(conn):
        [token] = db.list_tokens(conn, "alice@example.com")
        db.revoke_token(conn, token["id"])

    thread = _later(shared_settings, 0.6, revoke)
    start = time.monotonic()
    r = shared_client.get("/api/reports/prorate/wait?timeout=10", headers=alice)
    thread.join()
    assert time.monotonic() - start < 3
    assert (r.status_code, r.json()) == (401, {"detail": "invalid token"})


def test_reply_response_shows_the_thread_state_after_the_reply(
    shared_client, shared_settings, alice
):
    push(shared_client, auth=alice)
    url = "/api/reports/prorate/comments"
    agent_thread = shared_client.post(url, json={"body": "Note"}, headers=alice).json()["id"]
    with closing(db.connect(shared_settings.database_url)) as conn:
        human_thread = db.add_comment(
            conn,
            report_id="prorate",
            version=1,
            author_id="bob@example.com",
            author_name="Bob",
            body="Why?",
        )["id"]
        db.resolve_thread(conn, "prorate", human_thread, by="bob@example.com")
        db.resolve_thread(conn, "prorate", agent_thread, by="alice@example.com")
    kept = shared_client.post(url, json={"body": "Fixed", "parent_id": human_thread}, headers=alice)
    assert (kept.json()["resolved"], kept.json()["resolved_by"]) == (True, "bob@example.com")
    opened = shared_client.post(
        url, json={"body": "Again", "parent_id": agent_thread}, headers=alice
    )
    assert (opened.json()["resolved"], opened.json()["resolved_by"]) == (False, None)
    threads = {t["id"]: t for t in shared_client.get(url, headers=alice).json()["threads"]}
    [reply] = threads[human_thread]["replies"]
    assert {k: reply[k] for k in ("resolved", "resolved_by")} == {
        "resolved": True,
        "resolved_by": "bob@example.com",
    }


def test_shared_mode_token_refusals_are_logged_with_a_reason(
    shared_client, shared_settings, alice, bob, caplog
):
    caplog.set_level("INFO", logger="verifair")
    good = alice["Authorization"].removeprefix("Bearer ")
    with closing(db.connect(shared_settings.database_url)) as conn:
        revoked, row = db.create_token(conn, owner_id="alice@example.com", label="old")
        db.revoke_token(conn, row["id"])
        conn.execute("UPDATE users SET active=0 WHERE id='bob@example.com'")
    for headers in [
        {},
        {"Authorization": "Token abc"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer t"},
        {"Authorization": "Bearer vf_nodot"},
        {"Authorization": f"Bearer {good}x"},
        {"Authorization": f"Bearer {revoked}"},
        bob,
    ]:
        assert shared_client.get("/api/reports/x", headers=headers).status_code == 401
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("api token")]
    assert lines == [
        f"api token refused reason={reason} path='/api/reports/x'"
        for reason in (
            "missing",
            "malformed",
            "malformed",
            "shared-token",
            "malformed",
            "unknown",
            "revoked",
            "inactive",
        )
    ]
    assert all(r.levelname == "INFO" for r in caplog.records if "api token" in r.getMessage())
    for secret in (good, revoked, bob["Authorization"].removeprefix("Bearer ")):
        assert secret.partition(".")[2] not in caplog.text


def test_shared_mode_wait_logs_why_the_token_stopped_working(
    shared_client, shared_settings, alice, caplog
):
    caplog.set_level("INFO", logger="verifair")
    push(shared_client, auth=alice)

    def revoke(conn):
        [token] = db.list_tokens(conn, "alice@example.com")
        db.revoke_token(conn, token["id"])

    thread = _later(shared_settings, 0.6, revoke)
    r = shared_client.get("/api/reports/prorate/wait?timeout=10", headers=alice)
    thread.join()
    assert r.status_code == 401
    assert "api token refused reason=revoked path='/api/reports/prorate/wait'" in caplog.text


def test_shared_mode_token_refusal_logs_escape_control_characters(
    shared_client, shared_settings, alice, caplog
):
    with closing(db.connect(shared_settings.database_url)) as conn:
        db.push_report(
            conn,
            project="p",
            title="T",
            risk="low",
            body="## A\nx",
            id="\x1b[2K",
            author_id="alice@example.com",
        )
    caplog.set_level("INFO", logger="verifair")

    def revoke(conn):
        [token] = db.list_tokens(conn, "alice@example.com")
        db.revoke_token(conn, token["id"])

    assert shared_client.get("/api/reports/%1B%5B2K").status_code == 401
    thread = _later(shared_settings, 0.6, revoke)
    r = shared_client.get("/api/reports/%1B%5B2K/wait?timeout=10", headers=alice)
    thread.join()
    assert r.status_code == 401
    assert "api token refused reason=missing path='/api/reports/\\x1b[2K'" in caplog.text
    assert "api token refused reason=revoked path='/api/reports/\\x1b[2K/wait'" in caplog.text
    assert "\x1b" not in caplog.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "a\x00b"},
        {"body": "## Intent\n\x00"},
        {"plan": "# Plan\n\x00"},
        {"author": "bot\x00"},
        {"agent": "\x00"},
        {"model": "m\x00"},
        {"author_id": "a\x00@example.com"},
        {"reviewers": ["r\x00@example.com"]},
        {"links": [{"label": "MR\x00", "url": "https://mr/1"}]},
        {"ticket_url": "https://t/\x00"},
    ],
)
@pytest.mark.parametrize("bad", ["\x00", "\ud800", "\udfff"])
def test_push_with_an_unstorable_character_is_422(client, overrides, bad):
    payload = {"project": "billing-service", "title": "Prorate", "risk": "high", "body": SAMPLE}
    assert _post_json(client, "/api/reports", payload | overrides, bad).status_code == 422


def _post_json(client, path, payload, bad):
    # Postgres text holds neither NUL nor a lone surrogate. The payload marks the spot with
    # NUL, and json.dumps writes the swapped-in character as an escape, as a client would.
    text = json.dumps(payload).replace("\\u0000", json.dumps(bad)[1:-1])
    return client.post(path, content=text, headers=H | {"Content-Type": "application/json"})


@pytest.mark.parametrize("field", ["body", "author", "section", "quote", "prefix", "suffix"])
@pytest.mark.parametrize("bad", ["\x00", "\ud800"])
def test_comment_with_an_unstorable_character_is_422(client, field, bad):
    push(client)
    payload = {"body": "Why?", "section": "Intent", "quote": "Full new price."}
    payload |= {field: payload.get(field, "x") + "\x00"}
    assert _post_json(client, "/api/reports/prorate/comments", payload, bad).status_code == 422


def test_nul_character_in_a_report_id_is_404(client):
    push(client)
    for path in ("/api/reports/pro%00rate", "/api/reports/pro%00rate/comments"):
        assert client.get(path, headers=H).status_code == 404, path


def test_a_nan_in_the_payload_gets_a_strict_json_422(client):
    text = '{"project": "p", "title": "T", "risk": NaN, "body": "b"}'
    response = client.post(
        "/api/reports", content=text, headers=H | {"Content-Type": "application/json"}
    )

    def refuse(constant):
        raise ValueError(constant)

    assert response.status_code == 422
    detail = json.loads(response.text, parse_constant=refuse)["detail"]
    assert [(e["loc"], "input" in e) for e in detail] == [(["body", "risk"], False)]


def _plan_push(client, auth=H, **changes):
    payload = {"id": "prorate", "project": "billing-service", "title": "Prorate", "risk": "high"}
    return client.post("/api/reports", json=payload | changes, headers=auth)


def _approve_plan(settings, revision=1):
    with closing(db.connect(settings.database_url)) as conn:
        db.sign_version(
            conn,
            report_id="prorate",
            version=revision,
            target="plan",
            signer_id="owner000",
            signer_name="Owner",
        )
        db.add_verdict(
            conn,
            report_id="prorate",
            version=revision,
            target="plan",
            reviewer_id="reviewer",
            reviewer_name="Reviewer",
            verdict="approve",
        )


def test_plan_first_api_roundtrip(strict_client, settings):
    plan = "  # Návrh 🦊\r\n\nOne paragraph.  \n"
    created = _plan_push(strict_client, workflow="plan_first", plan=plan)
    assert (created.status_code, created.json()) == (
        201,
        {"id": "prorate", "version": 1, "url": "http://localhost:8000/r/prorate"},
    )
    initial = strict_client.get("/api/reports/prorate", headers=H).json()
    assert (initial["workflow"], initial["stage"], initial["status"]) == (
        "plan_first",
        "plan",
        "unsigned",
    )
    assert initial["implementation_status"] is initial["signed_by"] is initial["signed_at"] is None
    assert initial["verdicts"] == []
    assert initial["plan_review"] == {
        "revision": 1,
        "status": "unsigned",
        "signed_by": None,
        "signed_at": None,
        "open_blocking": 0,
        "verdicts": [],
    }
    first = strict_client.get("/api/reports/prorate/versions/1", headers=H).json()
    assert first == {
        "version": 1,
        "created_at": initial["versions"][0]["created_at"],
        "commit": None,
        "body": None,
        "plan": plan,
        "plan_revision": 1,
    }
    _approve_plan(settings)
    ready = strict_client.get("/api/reports/prorate", headers=H).json()
    assert (ready["stage"], ready["status"]) == (
        "awaiting_implementation",
        "awaiting_implementation",
    )
    body = "## Verification\nPassed.\n\n## Rollback\nRevert.\n\n## Security impact\nNone.\n"
    attached = _plan_push(strict_client, expected_version=1, body=body, commit="abcdef0")
    assert attached.json() == created.json() | {"version": 2}
    metadata = strict_client.get("/api/reports/prorate", headers=H).json()
    assert (metadata["stage"], metadata["status"], metadata["implementation_status"]) == (
        "implementation",
        "unsigned",
        "unsigned",
    )
    assert [v["has_implementation"] for v in metadata["versions"]] == [False, True]
    assert [v["plan_revision"] for v in metadata["versions"]] == [1, 1]
    assert metadata["plan_review"]["verdicts"][0]["current"] is True
    assert metadata["plan_review"]["verdicts"][0]["target"] == "plan"
    assert metadata["verdicts"] == []
    second = strict_client.get("/api/reports/prorate/versions/2", headers=H).json()
    assert (second["body"], second["plan"], second["commit"], second["plan_revision"]) == (
        body,
        plan,
        "abcdef0",
        1,
    )
    with closing(db.connect(settings.database_url)) as conn:
        db.sign_version(
            conn, report_id="prorate", version=2, signer_id="owner000", signer_name="Owner"
        )
        db.add_verdict(
            conn,
            report_id="prorate",
            version=2,
            reviewer_id="reviewer",
            reviewer_name="Reviewer",
            verdict="approve",
        )
        blocker = db.add_comment(
            conn,
            report_id="prorate",
            target="plan",
            version=1,
            author_id="reviewer",
            author_name="Reviewer",
            body="Reconsider",
        )
    paused = strict_client.get("/api/reports/prorate", headers=H).json()
    assert (paused["status"], paused["implementation_status"]) == ("changes_requested", "approved")
    assert paused["verdicts"][0]["target"] == "implementation"
    with closing(db.connect(settings.database_url)) as conn:
        db.resolve_thread(conn, "prorate", blocker["id"])
    assert strict_client.get("/api/reports/prorate", headers=H).json()["status"] == "approved"
    revised = _plan_push(strict_client, expected_version=2, plan="# Revised\nDifferent.")
    assert revised.json()["version"] == 3
    latest = strict_client.get("/api/reports/prorate", headers=H).json()
    assert (latest["stage"], latest["status"], latest["plan_review"]["revision"]) == (
        "plan",
        "unsigned",
        3,
    )
    assert latest["plan_review"]["verdicts"][0]["current"] is False
    assert latest["verdicts"][0]["current"] is False
    assert strict_client.get("/api/reports/prorate/versions/1", headers=H).json() == first
    third = strict_client.get("/api/reports/prorate/versions/3", headers=H).json()
    assert (third["body"], third["commit"]) == (body, "abcdef0")
    assert strict_client.get("/api/reports/prorate/versions/99", headers=H).status_code == 404
    assert strict_client.get("/api/reports/missing/versions/1", headers=H).status_code == 404
    assert strict_client.get("/api/reports/prorate/versions/1").status_code == 401


@pytest.mark.parametrize(
    "changes, status, error",
    [
        ({"plan": None, "expected_version": 1}, 422, "plan-first reviews require plan"),
        ({"body": None, "expected_version": 1}, 422, "implementation cannot be removed"),
        ({"plan": "New"}, 422, "expected_version is required for plan-first updates"),
        ({"expected_version": 1}, 422, "at least one content field must be supplied"),
        (
            {"plan": "New", "expected_version": 1, "commit": "abcdef0"},
            422,
            "commit requires supplied body",
        ),
        (
            {"workflow": "artifact", "body": SAMPLE, "expected_version": 1},
            409,
            "workflow cannot be changed",
        ),
        (
            {"plan": "New", "expected_version": 2},
            409,
            {"detail": "review changed", "current_version": 1},
        ),
        (
            {"body": SAMPLE, "expected_version": 1},
            409,
            {"detail": "current plan is not approved", "plan_revision": 1},
        ),
    ],
)
def test_plan_input_contract(client, settings, changes, status, error):
    assert _plan_push(client, workflow="plan_first", plan="# Original").status_code == 201
    with closing(db.connect(settings.database_url)) as conn:
        before = db.get_report(conn, "prorate"), db.list_versions(conn, "prorate")
    response = _plan_push(client, title="Must roll back", reviewers=["unexpected"], **changes)
    assert (response.status_code, response.json()) == (
        status,
        error if isinstance(error, dict) else {"detail": error},
    )
    with closing(db.connect(settings.database_url)) as conn:
        assert (db.get_report(conn, "prorate"), db.list_versions(conn, "prorate")) == before
        assert db.requested_reviewers(conn, "prorate") == []


@pytest.mark.parametrize(
    "changes",
    [
        {"body": SAMPLE},
        {"expected_version": 1},
        {"plan": None},
        {"plan": ""},
        {"plan": " \n"},
        {"plan": "x\x00"},
        {"plan": "x\ud800"},
        {"workflow": "unknown"},
        {"commit": "abcdef0"},
    ],
)
def test_plan_initial_input_contract(client, settings, changes):
    payload = {"workflow": "plan_first", "plan": "# Original"} | changes
    response = client.post(
        "/api/reports",
        content=json.dumps(
            {"id": "prorate", "project": "billing-service", "title": "Prorate", "risk": "high"}
            | payload
        ),
        headers=H | {"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    with closing(db.connect(settings.database_url)) as conn:
        assert db.get_report(conn, "prorate") is None


def test_plan_explicit_null_creation_and_omission_inherit(client, settings):
    assert (
        _plan_push(client, workflow="plan_first", plan="# Original", body=None).status_code == 201
    )
    _approve_plan(settings)
    assert _plan_push(client, expected_version=1, plan="# Original", body=SAMPLE).status_code == 201
    assert _plan_push(client, expected_version=2, plan="# Original").status_code == 201
    third = client.get("/api/reports/prorate/versions/3", headers=H).json()
    assert (third["body"], third["plan"], third["plan_revision"]) == (SAMPLE, "# Original", 1)
    refused = _plan_push(client, expected_version=3, plan="# Different", body=SAMPLE)
    assert (refused.status_code, refused.json()) == (
        409,
        {"detail": "current plan is not approved", "plan_revision": 4},
    )
    oversized = _plan_push(client, expected_version=3, plan="é" * 600000)
    assert oversized.status_code == 413
    assert client.get("/api/reports/prorate", headers=H).json()["current_version"] == 3


def test_plan_comments_and_wait_target(client, settings):
    assert _plan_push(client, workflow="plan_first", plan="## Design\nOriginal.").status_code == 201
    empty = client.get("/api/reports/prorate/comments", headers=H).json()
    assert empty == {
        "threads": [],
        "current_version": 1,
        "target": "implementation",
        "revision": None,
    }
    assert _comment(client).status_code == 409
    missing_guard = _comment(client, target="plan")
    assert (missing_guard.status_code, missing_guard.json()) == (
        422,
        {"detail": "plan revision is required"},
    )
    root = _comment(
        client, target="plan", revision=1, section="Design", quote="Original.", blocking=False
    )
    assert root.status_code == 201
    root = root.json()
    assert (root["target"], root["version"]) == ("plan", 1)
    _approve_plan(settings)
    assert _plan_push(client, expected_version=1, body=SAMPLE).status_code == 201
    threads = client.get("/api/reports/prorate/comments?target=plan", headers=H).json()
    assert (threads["current_version"], threads["target"], threads["revision"]) == (2, "plan", 1)
    assert [t["id"] for t in threads["threads"]] == [root["id"]]
    assert threads["threads"][0]["stale"] is False
    assert client.get("/api/reports/prorate/comments", headers=H).json() == {
        "threads": [],
        "current_version": 2,
        "target": "implementation",
        "revision": 2,
    }
    for kind, key in [("comment", "comment"), ("verdict", "verdict")]:
        event = _wait(
            client, f"target=plan&for={kind}&since=2000-01-01T00:00:00Z&timeout=0.01"
        ).json()
        assert (event["event"], event["current_version"], event["target"], event["revision"]) == (
            kind,
            2,
            "plan",
            1,
        )
        assert (event[key]["target"], event[key]["version"]) == ("plan", 1)
    timeout = _wait(client, "since=2000-01-01T00:00:00Z&timeout=0.01").json()
    assert (timeout["event"], timeout["revision"]) == ("timeout", 2)
    conflict = _comment(client, parent_id=root["id"], target="implementation")
    assert (conflict.status_code, conflict.json()) == (
        422,
        {"detail": "reply target conflicts with root target"},
    )
    assert _plan_push(client, expected_version=2, plan="## Different\nNew.").status_code == 201
    stale = _comment(client, parent_id=root["id"], revision=1)
    assert (stale.status_code, stale.json()) == (
        409,
        {"detail": "document changed", "target": "plan", "current_revision": 3},
    )
    reply = _comment(client, parent_id=root["id"], revision=3)
    assert reply.status_code == 201
    assert (reply.json()["target"], reply.json()["version"], reply.json()["section"]) == (
        "plan",
        1,
        "Design",
    )
    assert (
        client.post(f"/api/reports/prorate/comments/{root['id']}/resolve", headers=H).status_code
        == 200
    )
    timeout = _wait(client, "target=plan&since=2000-01-01T00:00:00Z&timeout=0.01").json()
    assert (timeout["event"], timeout["current_version"], timeout["revision"]) == ("timeout", 3, 3)


def test_plan_artifact_feedback_is_read_only(client):
    assert push(client, plan="# Context").status_code == 201
    for response in [
        client.get("/api/reports/prorate/comments?target=plan", headers=H),
        _wait(client, "target=plan&timeout=0.01"),
        _comment(client, target="plan", revision=1),
    ]:
        assert (response.status_code, response.json()) == (
            422,
            {"detail": "artifact plans are read-only"},
        )
    assert client.get("/api/reports/prorate/versions/1", headers=H).json()["plan"] == "# Context"


def test_plan_api_owner_rules(shared_client, shared_settings, alice, bob, admin):
    assert (
        _plan_push(shared_client, auth=alice, workflow="plan_first", plan="# Plan").status_code
        == 201
    )
    for auth in [bob, admin]:
        response = _plan_push(
            shared_client, auth=auth, workflow="artifact", expected_version=1, body=SAMPLE
        )
        assert (response.status_code, response.json()) == (403, {"detail": NOT_OWNER})
        response = shared_client.post(
            "/api/reports/prorate/comments",
            headers=auth,
            json={"body": "Intruder", "target": "plan", "revision": 99},
        )
        assert (response.status_code, response.json()) == (403, {"detail": NOT_OWNER})
    with closing(db.connect(shared_settings.database_url)) as conn:
        root = db.add_comment(
            conn,
            report_id="prorate",
            target="plan",
            version=1,
            author_id="bob@example.com",
            author_name="Bob",
            body="Human concern",
        )
        db.resolve_thread(conn, "prorate", root["id"], by="bob@example.com")
    reply = shared_client.post(
        "/api/reports/prorate/comments",
        headers=alice,
        json={"body": "Answer", "parent_id": root["id"]},
    )
    assert (reply.status_code, reply.json()["resolved"], reply.json()["target"]) == (
        201,
        True,
        "plan",
    )
    resolve = shared_client.post(
        f"/api/reports/prorate/comments/{root['id']}/resolve", headers=alice
    )
    assert (resolve.status_code, resolve.json()) == (403, {"detail": AGENT_RESOLVE})
    for action in ["sign", "verdict"]:
        assert shared_client.post(
            f"/api/reports/prorate/{action}", headers=alice, json={"target": "plan", "revision": 1}
        ).status_code in (404, 405)
    assert (
        shared_client.get("/api/reports/prorate/comments?target=plan", headers=bob).status_code
        == 200
    )


@pytest.mark.parametrize("anchor", ["section", "quote"])
def test_plan_comment_validation_uses_locked_current_markdown(
    client, settings, monkeypatch, anchor
):
    assert _plan_push(client, workflow="plan_first", plan="# Plan").status_code == 201
    _approve_plan(settings)
    assert _plan_push(client, expected_version=1, body=SAMPLE).status_code == 201
    add_comment = db.add_comment

    def update_before_lock(conn, **kwargs):
        with closing(db.connect(settings.database_url)) as writer:
            db.push_report(
                writer,
                id="prorate",
                project="billing-service",
                title="Prorate",
                risk="high",
                body="## Other\nChanged." if anchor == "section" else "## Intent\nChanged.",
                expected_version=2,
            )
        return add_comment(conn, **kwargs)

    monkeypatch.setattr(db, "add_comment", update_before_lock)
    response = _comment(client, section="Intent", quote="Full new price.")
    assert response.status_code == 422
    assert response.json() == (
        {"detail": "unknown section", "sections": ["Other"]}
        if anchor == "section"
        else {"detail": "quote not found"}
    )
    metadata = client.get("/api/reports/prorate/comments", headers=H).json()
    assert metadata == {
        "threads": [],
        "current_version": 3,
        "target": "implementation",
        "revision": 3,
    }


@pytest.mark.parametrize(
    "path",
    ["", "/comments?target=plan", "/wait?target=plan&since=2000-01-01T00:00:00Z&timeout=0.01"],
)
def test_plan_api_read_identity_is_one_snapshot(client, settings, monkeypatch, path):
    assert _plan_push(client, workflow="plan_first", plan="## Design\nOriginal.").status_code == 201
    root = _comment(client, target="plan", revision=1, body="Old feedback", blocking=False).json()
    get_report = db.get_report
    pushed = False

    def update_after_metadata_read(conn, id):
        nonlocal pushed
        report = get_report(conn, id)
        if not pushed:
            pushed = True
            with closing(db.connect(settings.database_url)) as writer:
                db.push_report(
                    writer,
                    id="prorate",
                    project="billing-service",
                    title="Changed",
                    risk="high",
                    body=None,
                    body_supplied=False,
                    plan="## Different\nReplacement.",
                    expected_version=1,
                )
        return report

    monkeypatch.setattr(db, "get_report", update_after_metadata_read)
    response = client.get(f"/api/reports/prorate{path}", headers=H).json()
    assert response["current_version"] == 1
    if not path:
        assert (response["title"], response["plan_review"]["revision"]) == ("Prorate", 1)
        assert [v["version"] for v in response["versions"]] == [1]
    elif path.startswith("/comments"):
        assert response["revision"] == 1
        assert [t["id"] for t in response["threads"]] == [root["id"]]
    else:
        assert (response["event"], response["revision"]) == ("comment", 1)
        assert response["comment"]["id"] == root["id"]
    latest = client.get("/api/reports/prorate", headers=H).json()
    assert (latest["current_version"], latest["plan_review"]["revision"]) == (2, 2)


def test_plan_wait_timeout_follows_push_and_has_no_synthetic_events(client, settings):
    assert _plan_push(client, workflow="plan_first", plan="# Original").status_code == 201

    def update(conn):
        db.sign_version(
            conn,
            report_id="prorate",
            target="plan",
            version=1,
            signer_id="owner000",
            signer_name="Owner",
        )
        db.push_report(
            conn,
            id="prorate",
            project="billing-service",
            title="Changed",
            risk="high",
            body=None,
            body_supplied=False,
            plan="# Changed",
            expected_version=1,
        )

    worker = _later(settings, 0.02, update)
    response = _wait(client, "target=plan&timeout=0.15").json()
    worker.join()
    assert (
        response["event"],
        response["current_version"],
        response["target"],
        response["revision"],
    ) == ("timeout", 2, "plan", 2)
    absent = _wait(client, "timeout=0.01").json()
    assert (absent["event"], absent["current_version"], absent["revision"]) == ("timeout", 2, None)


def test_plan_draft_notifications_follow_resulting_stage(client, settings, monkeypatch):
    from verifair import notify

    sent = []
    monkeypatch.setattr(notify, "send", lambda settings, text: sent.append(text))
    assert _plan_push(client, workflow="plan_first", plan="# Plan").status_code == 201
    _approve_plan(settings)
    assert _plan_push(client, expected_version=1, body=SAMPLE).status_code == 201
    assert _plan_push(client, expected_version=2, plan="# Changed").status_code == 201
    assert sent == [
        "Draft plan revision 1 in billing-service: Prorate http://localhost:8000/r/prorate?tab=plan",
        "Draft implementation v2 in billing-service: Prorate http://localhost:8000/r/prorate?tab=implementation",
        "Draft plan revision 3 in billing-service: Prorate http://localhost:8000/r/prorate?tab=plan",
    ]


def test_plan_openapi_contract(client):
    schema = client.get("/openapi.json").json()
    report = schema["components"]["schemas"]["ReportIn"]
    assert report["required"] == ["project", "title", "risk"]
    for name in ["body", "plan", "workflow", "expected_version"]:
        assert {item.get("type") for item in report["properties"][name]["anyOf"]} >= {"null"}
    assert report["properties"]["workflow"]["anyOf"][0]["enum"] == ["artifact", "plan_first"]
    assert report["properties"]["expected_version"]["anyOf"][0]["exclusiveMinimum"] == 0
    comment = schema["components"]["schemas"]["CommentIn"]
    assert comment["properties"]["target"]["anyOf"][0]["enum"] == ["implementation", "plan"]
    assert comment["properties"]["revision"]["anyOf"][0]["exclusiveMinimum"] == 0
    for operation in ["comments", "wait"]:
        target = next(
            p
            for p in schema["paths"][f"/api/reports/{{id}}/{operation}"]["get"]["parameters"]
            if p["name"] == "target"
        )
        assert target["schema"]["enum"] == ["implementation", "plan"]
        assert target["schema"]["default"] == "implementation"
    assert "/api/reports/{id}/versions/{n}" in schema["paths"]


@pytest.mark.parametrize("workflow", ["plan_first", "artifact"])
def test_push_notification_keeps_its_snapshot_during_concurrent_push(
    client, settings, monkeypatch, workflow
):
    from verifair import notify

    if workflow == "plan_first":
        assert _plan_push(client, workflow=workflow, plan="# Original").status_code == 201
        _approve_plan(settings)
    else:
        assert push(client, id="prorate").status_code == 201
    sent = []
    monkeypatch.setattr(notify, "send", lambda settings, text: sent.append(text))
    real_push = db.push_report

    def push_then_another_writer(conn, **kwargs):
        result = real_push(conn, **kwargs)
        assert result == ("prorate", 2)
        with closing(db.connect(settings.database_url)) as writer:
            later_content = (
                {"body": None, "body_supplied": False, "plan": "# Later plan"}
                if workflow == "plan_first"
                else {"body": "## Intent\nLater implementation."}
            )
            assert real_push(
                writer,
                id="prorate",
                project="billing-service",
                title="Prorate",
                risk="high",
                expected_version=2,
                **later_content,
            ) == ("prorate", 3)
        return result

    monkeypatch.setattr(db, "push_report", push_then_another_writer)
    response = _plan_push(client, expected_version=1, body=SAMPLE)
    assert (response.status_code, response.json()) == (
        201,
        {"id": "prorate", "version": 2, "url": "http://localhost:8000/r/prorate"},
    )
    expected = (
        "Draft implementation v2 in billing-service: Prorate "
        "http://localhost:8000/r/prorate?tab=implementation"
        if workflow == "plan_first"
        else "Draft report v2 in billing-service: Prorate http://localhost:8000/r/prorate"
    )
    assert sent == [expected]
    latest = client.get("/api/reports/prorate", headers=H).json()
    assert latest["current_version"] == 3
    if workflow == "plan_first":
        assert (latest["stage"], latest["plan_review"]["revision"]) == ("plan", 3)
