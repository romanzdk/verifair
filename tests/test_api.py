import pytest

H = {"Authorization": "Bearer t"}
SAMPLE = "## Intent\nFull new price.\n\n## Edge cases\nFirst day."


def push(client, **overrides):
    payload = {"project": "billing-service", "title": "Prorate", "risk": "high", "body": SAMPLE}
    return client.post("/api/reports", json=payload | overrides, headers=H)


def test_requires_token(client):
    r = client.post("/api/reports", json={})
    assert (r.status_code, r.json()) == (401, {"detail": "invalid token"})
    bad = client.get("/api/reports/x", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


def test_push_and_get_report(client):
    r = push(
        client, ticket_url="https://t/BILL-412", links=[{"label": "MR", "url": "https://mr/1"}]
    )
    assert r.status_code == 201
    assert r.json() == {"id": "prorate", "version": 1, "url": "http://localhost:8000/r/prorate"}
    g = client.get("/api/reports/prorate", headers=H).json()
    assert (g["status"], g["current_version"], g["risk"]) == ("awaiting_review", 1, "high")
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
        "resolved": False,
    }
    reply = client.post(
        "/api/reports/prorate/comments", json={"body": "Because.", "parent_id": cid}, headers=H
    )
    assert reply.status_code == 201
    assert set(reply.json()) == set(root.json()) and reply.json()["parent_id"] == cid

    threads = client.get("/api/reports/prorate/comments", headers=H).json()["threads"]
    assert len(threads) == 1
    t = threads[0]
    assert {k: v for k, v in t.items() if k != "replies"} == root.json() | {
        "stale": False,
        "found": True,
    }
    assert t["replies"] == [reply.json() | {"stale": False, "found": True}]

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
    assert sent[0].startswith("New report v1 in billing-service")
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


@pytest.mark.parametrize("anchor", [{"section": "Intent"}, {"quote": "Full"}])
def test_reply_with_section_or_quote_422(client, anchor):
    push(client)
    root = _comment(client).json()["id"]
    r = _comment(client, parent_id=root, **anchor)
    assert (r.status_code, r.json()) == (422, {"detail": "replies cannot set section or quote"})
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
