import asyncio
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from verifair import db
from verifair.app import create_app

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


def test_missing_shared_token_never_authenticates(settings):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from verifair.app import create_app

    shared = replace(
        settings, api_token=None, identity_header="X-Forwarded-Email", bootstrap_admin_id="boss"
    )
    client = TestClient(create_app(shared))
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
    assert only_open == {"threads": []}
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
    assert client.get("/api/reports/prorate/comments", headers=H).json() == {"threads": []}


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
    conn = connect(settings.db_path)
    conn.execute("UPDATE comments SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (reply,))
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
        "verdict": "approve",
        "version": 1,
        "summary": "",
        "created_at": v["created_at"],
        "current": False,
    }


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


def test_push_and_comment_notify_when_slack_configured(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from verifair import notify
    from verifair.app import create_app
    from verifair.config import Settings

    sent = []
    monkeypatch.setattr(notify, "send", lambda settings, text: sent.append(text))
    settings = Settings(
        api_token="t",
        db_path=str(tmp_path / "n.db"),
        base_url="http://localhost:8000",
        slack_webhook_url="https://hooks.example/x",
    )
    client = TestClient(create_app(settings))
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
    assert client.get("/api/reports/prorate/comments", headers=H).json() == {"threads": []}


def test_blank_title_says_must_not_be_blank(client):
    r = push(client, title="   ")
    assert r.status_code == 422
    assert "must not be blank" in r.json()["detail"][0]["msg"]


@pytest.fixture
def strict_client(settings):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from verifair.app import create_app

    required = ("Verification", "Rollback", "Security impact")
    return TestClient(create_app(replace(settings, required_sections=required)))


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
def test_synthetic_ui_sections_do_not_satisfy_required_headings(settings, required):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from verifair.app import create_app

    client = TestClient(create_app(replace(settings, required_sections=(required,))))
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
    with closing(db.connect(settings.db_path)) as conn:
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
    conn = db.connect(settings.db_path)
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
    assert client.get("/api/reports/prorate/comments", headers=H).json() == {"threads": []}
    assert _comment(client, section="Edge cases", quote="First day").status_code == 201


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
        conn = db.connect(settings.db_path)
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
    conn = db.connect(settings.db_path)
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
        _verdict(conn)
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


def test_wait_closes_every_poll_connection(client, monkeypatch):
    push(client)
    connections = []
    original_connect = db.connect

    def track_connect(path):
        conn = original_connect(path)
        connections.append(conn)
        return conn

    monkeypatch.setattr(db, "connect", track_connect)
    assert _wait(client, "timeout=0.1").json()["event"] == "timeout"
    assert len(connections) >= 2
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            conn.execute("SELECT 1")


def test_wait_without_since_detects_new_rows_in_the_same_second(client, settings, monkeypatch):
    monkeypatch.setattr(db, "_now", lambda: "2000-01-01T00:00:00+00:00")
    push(client)
    _comment(client)
    thread = _later(settings, 0.2, _root_comment)
    response = _wait(client, "timeout=2")
    thread.join()
    assert response.json()["comment"]["body"] == "Why?"
    assert response.json()["next_since"] == "2000-01-01T00:00:00+00:00"


def test_local_mode_refuses_an_empty_shared_token(settings):
    client = TestClient(create_app(replace(settings, api_token="")))
    for headers in [{"Authorization": "Bearer "}, {"Authorization": "Bearer"}, {}]:
        r = client.get("/api/reports/x", headers=headers)
        assert (r.status_code, r.json()) == (401, {"detail": "invalid token"}), headers


def _agent(settings, id, role="member"):
    with closing(db.connect(settings.db_path)) as conn:
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


def test_shared_mode_refuses_the_shared_token_and_accepts_member_tokens(shared_settings):
    client = TestClient(create_app(replace(shared_settings, api_token="t")))
    alice = _agent(shared_settings, "alice@example.com")
    for auth in (H, {"Authorization": "Bearer change-me"}):
        r = push(client, auth=auth)
        assert (r.status_code, r.json()) == (401, {"detail": SHARED_TOKEN})
    assert push(client, auth=alice).status_code == 201


def test_member_token_push_sets_owner_and_every_member_reads(shared_client, alice, bob):
    assert push(shared_client, auth=alice, author="Someone else").status_code == 201
    report = shared_client.get("/api/reports/prorate", headers=bob).json()
    assert (report["author"], report["author_id"]) == ("alice", "alice@example.com")
    assert shared_client.get("/api/reports/prorate/comments", headers=bob).json() == {"threads": []}
    waited = shared_client.get("/api/reports/prorate/wait?timeout=0.1", headers=bob)
    assert waited.json()["event"] == "timeout"
    again = push(shared_client, auth=alice, id="prorate", author_id="alice@example.com")
    assert again.json()["version"] == 2


def test_revoked_or_deactivated_tokens_are_refused_on_the_next_call(
    shared_client, shared_settings, alice, bob
):
    assert push(shared_client, auth=alice).status_code == 201
    assert shared_client.get("/api/reports/prorate", headers=bob).status_code == 200
    with closing(db.connect(shared_settings.db_path)) as conn:
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
    with closing(db.connect(shared_settings.db_path)) as conn:
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
    with closing(db.connect(shared_settings.db_path)) as conn:
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
    with closing(db.connect(shared_settings.db_path)) as conn:
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
    with closing(db.connect(shared_settings.db_path)) as conn:
        rows = {c["id"]: c for c in db.list_comments(conn, "prorate")}
    assert rows[agent_thread]["resolved_by"] == "agent"
    assert rows[human_thread]["resolved_at"] is rows[owners_thread]["resolved_at"] is None


def test_agents_cannot_comment_on_or_resolve_threads_of_unowned_reports(
    shared_client, shared_settings, alice
):
    with closing(db.connect(shared_settings.db_path)) as conn:
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
    with closing(db.connect(shared_settings.db_path)) as conn:
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
    with closing(db.connect(shared_settings.db_path)) as conn:
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
        f"api token refused reason={reason} path=/api/reports/x"
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
    assert "api token refused reason=revoked path=/api/reports/prorate/wait" in caplog.text
