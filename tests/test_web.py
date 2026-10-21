import re

import pytest

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
    client.post("/r/retry-webhooks/verdict", data={"version": 1, "verdict": "approve"})

    r = client.get("/p/billing-service")
    assert r.status_code == 200
    assert "Awaiting review · 1" in r.text and "Approved · 1" in r.text
    assert "Prorate" in r.text and "BILL-412" in r.text and "Retry webhooks" not in r.text

    approved = client.get("/p/billing-service?tab=approved")
    assert "Retry webhooks" in approved.text and "Prorate" not in approved.text
    everything = client.get("/p/billing-service?tab=all")
    assert "Retry webhooks" in everything.text and "Prorate" in everything.text

    assert client.get("/p/nope").status_code == 404
    assert "All · 2" in r.text
    bogus = client.get("/p/billing-service?tab=bogus")
    assert (
        bogus.status_code == 200 and "Prorate" in bogus.text and "Retry webhooks" not in bogus.text
    )


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

    resolved = client.post(f"/r/prorate/comments/{cid}/resolve")
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
    client.cookies.set("vf_name", "Alice")
    r = client.post(
        "/r/prorate/verdict",
        data={"version": 1, "verdict": "request_changes", "summary": "Round half-up."},
    )
    assert "You requested changes on v1" in r.text
    assert client.get("/api/reports/prorate", headers=H).json()["status"] == "changes_requested"


def test_verdict_approve_changes_status_and_sidebar(client):
    push(client)
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
    client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"})
    client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve", "summary": "Fine."})
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
    client.cookies.set("vf_id", "alice000")
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"})
    push(client, id="prorate", body="## Intent\nChanged.")

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
    assert page.count('<input type="hidden" name="version" value="1">') == 4
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
    client.post(f"/r/prorate/comments/{cid}/resolve")
    again = client.post(f"/r/prorate/comments/{cid}/resolve")
    assert again.status_code == 200 and 'class="thread resolved"' in again.text
    reopened = client.post(f"/r/prorate/comments/{cid}/reply", data={"version": 1, "body": "No."})
    assert 'class="thread resolved"' not in reopened.text and "No." in reopened.text
    assert api_threads(client)[0]["resolved"] is False


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
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve", "summary": "Fine."})
    current = client.get("/r/prorate").text
    assert "b-stale" not in current and "Fine." in current
    push(client, id="prorate", body="## Intent\nChanged.")
    page = client.get("/r/prorate").text
    assert 'class="badge b-approved b-stale"' in page and "approved v1" in page
    assert "Fine." in page
    old = client.get("/r/prorate/v/1").text
    assert ">Current: Awaiting review<" in old
    assert ">Awaiting review<" in page and "Current: " not in page


def test_report_page_a11y_attributes(client):
    push(client)
    client.cookies.set("vf_name", "Alice")
    client.post("/r/prorate/comments", data={"version": 1, "section": "Intent", "body": "Why?"})
    page = client.get("/r/prorate").text
    assert 'aria-label="Comment on Intent"' in page and 'aria-label="Comment on Edge cases"' in page
    assert 'aria-label="Reply"' in page and 'aria-label="Summary for the author"' in page
    assert 'aria-live="polite"' in page
    assert '<span class="n" role="img" aria-label="1 open thread">1</span>' in page
    assert page.count('hx-disabled-elt="find button"') == 3
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
    r = client.post(f"/r/prorate/comments/{cid}/reopen")
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
    client.cookies.set("vf_name", "Alice")
    refused = client.post("/r/prorate/verdict", data={"version": 1, "verdict": "request_changes"})
    assert f'<p class="error" role="alert">{REFUSED_COPY}</p>' in refused.text
    done = client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve"})
    assert '<p class="mine" role="alert">You approved v1</p>' in done.text


def test_app_js_caps_quote_and_maps_error_copy():
    from pathlib import Path

    import verifair

    js = (Path(verifair.__file__).parent / "static" / "app.js").read_text()
    assert "quote.slice(0, QUOTE_MAX)" in js and "const QUOTE_MAX = 2000;" in js
    for copy in [
        "Check the text and try again.",
        "This item no longer exists. Reload the page.",
        "Connection lost. Your text is still here, try again.",
        "Something went wrong (${status}). Reload and try again.",
    ]:
        assert copy in js
