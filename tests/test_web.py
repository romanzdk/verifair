import re

import pytest

H = {"Authorization": "Bearer t"}
REFUSED_COPY = "Add a summary or leave at least one open comment first."
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


def api_threads(client, id="prorate"):
    return client.get(f"/api/reports/{id}/comments", headers=H).json()["threads"]


def test_index_lists_projects_with_counts(client):
    push(client)
    r = client.get("/")
    assert r.status_code == 200
    assert "billing-service" in r.text and "Awaiting review" in r.text
    assert 'href="/p/billing-service"' in r.text


def test_project_page_tabs_and_unknown_404(client):
    push(client)
    push(client, title="Retry webhooks", risk="low", ticket_url=None)
    client.cookies.set("vf_name", "Alice")
    client.post("/r/retry-webhooks/verdict", data={"verdict": "approve"})

    r = client.get("/p/billing-service")
    assert r.status_code == 200
    assert "Awaiting review · 1" in r.text and "Approved · 1" in r.text
    assert "Prorate" in r.text and "BILL-412" in r.text and "Retry webhooks" not in r.text

    approved = client.get("/p/billing-service?tab=approved")
    assert "Retry webhooks" in approved.text and "Prorate" not in approved.text
    everything = client.get("/p/billing-service?tab=all")
    assert "Retry webhooks" in everything.text and "Prorate" in everything.text

    assert client.get("/p/nope").status_code == 404
    assert client.get("/p/billing-service?tab=bogus").status_code == 422


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
    assert 'class="badge b-awaiting_review"' in r.text and "Risk: high" in r.text
    assert client.get("/r/nope").status_code == 404


def test_comment_without_name_returns_prompt_and_stores_nothing(client):
    push(client)
    r = client.post("/r/prorate/comments", data={"section": "Intent", "body": "Why?"})
    assert r.status_code == 200
    assert "Set your name" in r.text and 'action="/me"' in r.text
    assert api_threads(client) == []


def test_comment_reply_resolve_via_htmx(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    r = client.post(
        "/r/prorate/comments",
        data={"section": "Edge cases", "quote": "first day", "body": "Which first day?"},
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

    reply = client.post(f"/r/prorate/comments/{cid}/reply", data={"body": "Cycle start."})
    assert "Which first day?" in reply.text and "Cycle start." in reply.text
    assert len(api_threads(client)[0]["replies"]) == 1

    resolved = client.post(f"/r/prorate/comments/{cid}/resolve")
    assert 'class="thread resolved"' in resolved.text
    assert api_threads(client)[0]["resolved"] is True

    page = client.get("/r/prorate")
    assert 'class="thread resolved"' in page.text and "0 open threads" in page.text
    assert client.post("/r/prorate/comments/999/reply", data={"body": "x"}).status_code == 404
    assert client.post("/r/prorate/comments/999/resolve").status_code == 404


def test_verdict_request_changes_refused_without_summary_or_thread(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    r = client.post("/r/prorate/verdict", data={"verdict": "request_changes", "summary": " "})
    assert r.status_code == 200
    assert REFUSED_COPY in r.text
    g = client.get("/api/reports/prorate", headers=H).json()
    assert (g["status"], g["verdicts"]) == ("awaiting_review", [])
    assert client.post("/r/prorate/verdict", data={"verdict": "maybe"}).status_code == 422


def test_verdict_request_changes_allowed_with_summary(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    r = client.post(
        "/r/prorate/verdict", data={"verdict": "request_changes", "summary": "Round half-up."}
    )
    assert "You requested changes on v1" in r.text
    assert client.get("/api/reports/prorate", headers=H).json()["status"] == "changes_requested"


def test_verdict_approve_changes_status_and_sidebar(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    r = client.post("/r/prorate/verdict", data={"verdict": "approve"})
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
    r = client.post("/r/prorate/comments", data={"section": "Intent", "body": "<b>x</b>"})
    assert "&lt;b&gt;x&lt;/b&gt;" in r.text and "<b>x</b>" not in r.text
    assert "&lt;b&gt;x&lt;/b&gt;" in client.get("/r/prorate").text


def test_web_posts_notify(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from verifair import notify
    from verifair.app import create_app
    from verifair.config import Settings

    sent = []
    monkeypatch.setattr(notify, "send", lambda settings, text: sent.append(text))
    settings = Settings("t", str(tmp_path / "n.db"), "http://localhost:8000", "https://hooks/x")
    client = TestClient(create_app(settings))
    push(client)
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/comments", data={"section": "Intent", "body": "Why?"})
    client.post("/r/prorate/verdict", data={"verdict": "approve", "summary": "Fine."})
    assert sent[1].startswith("Alice commented on Prorate › Intent: Why?")
    assert sent[2].startswith("Alice approved Prorate v1: Fine.")


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
    thread = client.post("/r/prorate/comments", data={"section": "Intent", "body": "Why?"})
    assert 'id="vf_t' in thread.text and 'id="vf_c' in thread.text
    r = client.get("/r/prorate")
    for slug in ["verdict", "sidebar", "popover", "t1", "cform-intent", "threads-intent"]:
        assert r.text.count(f'id="{slug}"') == 1, slug
    assert 'hx-post="/r/prorate/verdict"' in r.text and 'id="vf_verdict"' in r.text
    assert 'id="vf_cform_intent"' in r.text and 'hx-target="#vf_threads_intent"' in r.text


def test_blank_comment_and_reply_422(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    blank = client.post("/r/prorate/comments", data={"section": "Intent", "body": "  \n "})
    assert blank.status_code == 422 and api_threads(client) == []
    client.post("/r/prorate/comments", data={"section": "Intent", "body": "Why?"})
    cid = api_threads(client)[0]["id"]
    assert client.post(f"/r/prorate/comments/{cid}/reply", data={"body": " "}).status_code == 422
    assert api_threads(client)[0]["replies"] == []


def test_name_form_next_keeps_query(client):
    push(client)
    r = client.get("/p/billing-service?tab=approved")
    assert 'name="next" value="/p/billing-service?tab=approved"' in r.text


def test_comment_time_is_labelled_utc(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    r = client.post("/r/prorate/comments", data={"section": "Intent", "body": "Why?"})
    assert re.search(r'class="when">[^<]* \d\d:\d\d UTC<', r.text)


def test_first_visit_shows_inline_name_prompt(client):
    push(client)
    push(client, id="prorate", body="## Intent\nChanged.")
    r = client.get("/r/prorate")
    assert "Set your name</b>" in r.text
    assert r.text.index('<main class="main">') < r.text.index('class="name-prompt"')
    assert 'name="next" value="/r/prorate"' in r.text
    assert 'class="name-prompt"' not in client.get("/r/prorate/v/1").text
    client.cookies.set("vf_name", "Alice")
    assert 'class="name-prompt"' not in client.get("/r/prorate").text


@pytest.mark.parametrize("name", ["  ", "x" * 61])
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
    client.cookies.set("vf_id", "alice")
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/comments", data={"section": "Intent", "body": "Why?"})
    push(client, id="prorate", body="## Intent\nChanged.")

    client.cookies.set("vf_id", "bob")
    client.cookies.set("vf_name", "Bob")
    refused = client.post("/r/prorate/verdict", data={"verdict": "request_changes"})
    assert REFUSED_COPY in refused.text
    assert client.get("/api/reports/prorate", headers=H).json()["verdicts"] == []

    client.cookies.set("vf_id", "alice")
    client.cookies.set("vf_name", "Alice")
    allowed = client.post("/r/prorate/verdict", data={"verdict": "request_changes"})
    assert "You requested changes on v2" in allowed.text
    g = client.get("/api/reports/prorate", headers=H).json()
    assert (g["status"], g["verdicts"][0]["version"]) == ("changes_requested", 2)


def test_htmx_comment_without_name_retargets_form_and_stores_nothing(client):
    push(client)
    r = client.post(
        "/r/prorate/comments",
        data={"section": "Intent", "body": "Why?"},
        headers={"HX-Request": "true", "HX-Trigger": "vf_cform_intent"},
    )
    assert r.headers["HX-Retarget"] == "#vf_cform_intent"
    assert r.headers["HX-Reswap"] == "outerHTML"
    assert 'class="name-prompt"' in r.text
    assert api_threads(client) == []
