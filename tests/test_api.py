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
        json={"body": "Why?", "author": "bot", "section": "intent", "quote": "Full new price."},
        headers=H,
    )
    assert root.status_code == 201
    assert root.json()["body"] == "Why?" and root.json()["author"] == "bot"
    cid = root.json()["id"]
    reply = client.post(
        "/api/reports/prorate/comments", json={"body": "Because.", "parent_id": cid}, headers=H
    )
    assert reply.status_code == 201

    threads = client.get("/api/reports/prorate/comments", headers=H).json()["threads"]
    assert len(threads) == 1
    t = threads[0]
    assert (t["id"], t["version"], t["section"], t["quote"]) == (
        cid,
        1,
        "intent",
        "Full new price.",
    )
    assert (t["author"], t["body"], t["resolved"]) == ("bot", "Why?", False)
    assert [(x["author"], x["body"]) for x in t["replies"]] == [("agent", "Because.")]

    done = client.post(f"/api/reports/prorate/comments/{cid}/resolve", headers=H)
    assert (done.status_code, done.json()) == (200, {"resolved": True})
    only_open = client.get("/api/reports/prorate/comments?open=true", headers=H).json()
    assert only_open == {"threads": []}
    everything = client.get("/api/reports/prorate/comments?open=false", headers=H).json()
    assert len(everything["threads"]) == 1
    assert everything["threads"][0]["resolved"] is True
    assert len(everything["threads"][0]["replies"]) == 1


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
