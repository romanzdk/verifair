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


def test_index_lists_projects_with_counts(client):
    push(client)
    r = client.get("/")
    assert r.status_code == 200
    assert "billing-service" in r.text and "Awaiting review" in r.text
    assert 'href="/p/billing-service"' in r.text


def test_project_page_tabs_and_unknown_404(client):
    push(client)
    push(client, title="Retry webhooks", risk="low", ticket_url=None)
    push(client, title="Unsigned one", ticket_url=None)
    sign(client)
    sign(client, "retry-webhooks")
    client.cookies.set("vf_name", "Alice")
    client.post("/r/retry-webhooks/verdict", data={"version": 1, "verdict": "approve"})

    r = client.get("/p/billing-service")
    assert r.status_code == 200
    assert "Awaiting review · 1" in r.text and "Approved · 1" in r.text
    assert "Unsigned one" in r.text and "Retry webhooks" in r.text
    pending = client.get("/p/billing-service?tab=awaiting_review")
    assert "Prorate" in pending.text and "Unsigned one" not in pending.text
    assert "Retry webhooks" not in pending.text
    unsigned = client.get("/p/billing-service?tab=unsigned")
    assert "Unsigned one" in unsigned.text and "Prorate" not in unsigned.text
    assert "Prorate" in r.text and "BILL-412" in r.text

    approved = client.get("/p/billing-service?tab=approved")
    assert "Retry webhooks" in approved.text and "Prorate" not in approved.text
    everything = client.get("/p/billing-service?tab=all")
    assert "Retry webhooks" in everything.text and "Prorate" in everything.text

    assert client.get("/p/nope").status_code == 404
    assert "All statuses · 3" in r.text
    bogus = client.get("/p/billing-service?tab=bogus")
    assert bogus.status_code == 200 and "Prorate" in bogus.text and "Retry webhooks" in bogus.text


def test_unsigned_project_opens_with_its_changes_visible(client):
    push(client, title="Draft evidence report")
    page = client.get("/p/billing-service")
    assert "Draft evidence report" in page.text
    assert 'aria-current="page"' in page.text
    filtered = client.get("/p/billing-service?tab=awaiting_review")
    assert "No changes awaiting review" in filtered.text
    assert 'href="?tab=all"' in filtered.text


def test_project_cards_link_to_real_statuses_and_prioritize_reviewable_changes(client):
    push(client, title="Ready evidence")
    sign(client, "ready-evidence")
    push(client, title="Later unsigned report")
    page = client.get("/")
    assert 'href="/p/billing-service?tab=awaiting_review"' in page.text
    assert 'href="/p/billing-service?tab=unsigned"' in page.text
    assert 'href="/r/ready-evidence"' in page.text
    assert 'href="/r/later-unsigned-report"' not in page.text


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
    assert 'class="badge b-unsigned"' in r.text and "Risk: high" in r.text
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


def test_web_posts_notify(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from verifair import notify
    from verifair.app import create_app
    from verifair.config import Settings

    sent = []
    monkeypatch.setattr(notify, "send", lambda settings, text: sent.append(text))
    settings = Settings("t", str(tmp_path / "n.db"), "http://localhost:8000", "https://hooks/x")
    client = TestClient(create_app(settings), headers={"Sec-Fetch-Site": "same-origin"})
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
    assert page.count('<input type="hidden" name="version" value="1">') == 5
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
    assert "quote.slice(0, QUOTE_MAX)" in js and "const QUOTE_MAX = 2000;" in js
    for copy in [
        "Check the text and try again.",
        "This item no longer exists. Reload the page.",
        "Connection lost. Your text is still here, try again.",
        "Something went wrong (${status}). Reload and try again.",
    ]:
        assert copy in js


@pytest.fixture
def proxied(settings):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from verifair import db
    from verifair.app import create_app

    shared = replace(settings, identity_header="X-Forwarded-Email", bootstrap_admin_id="boss")
    app = create_app(shared)
    conn = db.connect(shared.db_path)
    for id in ("alice@example.com", "bob@example.com"):
        db.add_user(conn, id=id, name=id.partition("@")[0], role="member")
    conn.close()
    return TestClient(app, headers={"Sec-Fetch-Site": "same-origin"})


def test_identity_header_is_the_user(proxied):
    push(proxied)
    who = {"X-Forwarded-Email": "alice@example.com"}
    page = proxied.get("/r/prorate", headers=who)
    assert "Set your name" not in page.text and 'action="/me"' not in page.text
    assert '<span class="me">alice</span>' in page.text
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


def test_identity_header_blocks_set_name(proxied):
    r = proxied.post(
        "/me",
        data={"name": "Mallory"},
        headers={"X-Forwarded-Email": "alice@example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 403 and "vf_name" not in r.cookies


@pytest.mark.parametrize("value", [None, "", "  ", "agent"])
def test_identity_header_configured_but_absent_is_401(proxied, value):
    push(proxied)
    proxied.cookies.set("vf_id", "bob00000")
    proxied.cookies.set("vf_name", "Bob")
    headers = {} if value is None else {"X-Forwarded-Email": value}
    assert proxied.get("/r/prorate", headers=headers).status_code == 401
    assert proxied.post("/me", data={"name": "Bob"}, headers=headers).status_code == 401
    r = proxied.post(
        "/r/prorate/comments",
        data={"version": 1, "section": "Intent", "body": "Why?"},
        headers=headers,
    )
    assert r.status_code == 401
    assert api_threads(proxied) == []


SIGN_IN_COPY = "Sign in through your company login to use verifair."
CSP = (
    "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline';"
    " frame-ancestors 'none'"
)


def _anonymous(shared_settings):
    from fastapi.testclient import TestClient

    from verifair.app import create_app

    return TestClient(create_app(shared_settings))


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/"),
        ("get", "/p/x"),
        ("get", "/r/x"),
        ("get", "/docs"),
        ("get", "/redoc"),
        ("get", "/openapi.json"),
        ("get", "/missing"),
        ("post", "/me"),
        ("post", "/r/x/comments"),
    ],
)
def test_shared_mode_without_identity_is_401(shared_settings, method, path):
    r = getattr(_anonymous(shared_settings), method)(path)
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
    conn = db.connect(shared_settings.db_path)
    db.update_user(conn, "gone@example.com", active=False)
    conn.close()
    for who in ["gone@example.com", "stranger@example.com", "<b>x</b>@example.com"]:
        for r in [
            shared_client.get("/", headers={"X-Forwarded-Email": who}),
            shared_client.post("/me", data={"name": "M"}, headers={"X-Forwarded-Email": who}),
        ]:
            assert r.status_code == 403
            assert r.headers["content-type"].startswith("text/html")
            assert r.headers["content-security-policy"] == CSP
            escaped = who.replace("<", "&lt;").replace(">", "&gt;")
            copy = f"You are not a member of this workspace yet. Ask an admin to add {escaped}."
            assert copy in r.text
            assert "<b>" not in r.text


def test_shared_mode_member_is_the_users_row_and_gets_no_cookies(
    shared_client, shared_settings, as_user
):
    from verifair import db

    bob = as_user("bob@example.com", name="Bob Builder")
    conn = db.connect(shared_settings.db_path)
    db.push_report(conn, project="p", title="T", risk="low", body="## A\nx")
    conn.close()
    shared_client.cookies.set("vf_id", "mallory0")
    shared_client.cookies.set("vf_name", "Mallory")
    for path in ["/", "/p/p", "/r/t", "/r/t/v/1", "/docs", "/openapi.json"]:
        r = shared_client.get(path, headers=bob)
        assert r.status_code == 200, path
        assert "set-cookie" not in r.headers, path
    page = shared_client.get("/r/t", headers=bob).text
    assert '<span class="me">Bob Builder</span>' in page and "Mallory" not in page
    assert 'action="/me"' not in page
    r = shared_client.post(
        "/r/t/comments", data={"version": 1, "section": "A", "body": "Why?"}, headers=bob
    )
    assert r.status_code == 200 and "set-cookie" not in r.headers
    conn = db.connect(shared_settings.db_path)
    comment = db.list_comments(conn, "t")[0]
    conn.close()
    assert (comment["author_id"], comment["author_name"]) == ("bob@example.com", "Bob Builder")
    admin = shared_client.get("/").text
    assert '<span class="me">admin</span>' in admin


def test_shared_mode_browser_check_still_applies_to_members(shared_settings):
    r = _anonymous(shared_settings).post(
        "/me", data={"name": "M"}, headers={"X-Forwarded-Email": "admin@example.com"}
    )
    assert (r.status_code, r.json()) == (403, {"detail": "browser request required"})


def test_shared_mode_health_static_and_api_skip_the_membership_check(shared_settings):
    anonymous = _anonymous(shared_settings)
    assert anonymous.get("/healthz").json() == {"ok": True}
    assert anonymous.get("/static/app.css").status_code == 200
    assert anonymous.get("/api/reports/x").json() == {"detail": "invalid token"}


UNSIGNED_COPY = "The author has not signed this version yet."
SELF_COPY = "You signed this report. Someone else has to approve it."


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
    assert '<a class="ext" href="https://s/1">session</a>' in page
    assert "<code>0123456</code>" in page and "0123456789" not in page
    push(client, id="prorate", agent="claude-code")
    page = client.get("/r/prorate").text
    assert "Written by <b>claude-code</b><" in page and "session</a>" not in page
    push(client, id="prorate")
    assert "Written by" not in client.get("/r/prorate").text


def test_index_counts_unsigned(client):
    push(client)
    push(client, title="Other")
    sign(client)
    page = client.get("/").text
    assert page.count('aria-label="Unsigned: 1 change"') == 1
    assert page.count('aria-label="Awaiting review: 1 change"') == 1


AUTHOR_COPY = "Only alice@example.com can sign this report."


def test_identity_header_only_author_can_sign(proxied):
    push(proxied, author="alice@example.com")
    bob = {"X-Forwarded-Email": "bob@example.com"}
    page = proxied.get("/r/prorate", headers=bob).text
    assert AUTHOR_COPY in page and "Sign this version" not in page
    r = proxied.post("/r/prorate/sign", data={"version": 1}, headers=bob)
    assert r.status_code == 403 and AUTHOR_COPY in r.text
    assert api_report(proxied)["status"] == "unsigned"

    alice = {"X-Forwarded-Email": "alice@example.com"}
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


def test_identity_header_cookie_user_may_not_sign(proxied):
    push(proxied, author="alice@example.com")
    proxied.cookies.set("vf_name", "Bob")
    r = proxied.post("/r/prorate/sign", data={"version": 1})
    assert r.status_code == 401 and api_report(proxied)["status"] == "unsigned"


def test_identity_header_anyone_may_sign_agent_authored_report(proxied):
    push(proxied)
    r = proxied.post(
        "/r/prorate/sign", data={"version": 1}, headers={"X-Forwarded-Email": "bob@example.com"}
    )
    assert r.status_code == 200 and api_report(proxied)["signed_by"] == "bob"


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
        with closing(db.connect(settings.db_path)) as other:
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
    import sqlite3
    from contextlib import closing

    from verifair import db, rules

    push(client)
    sign(client)
    client.cookies.set("vf_name", "Bob")
    original = rules.can_approve
    attempted = []

    def try_concurrent_writer(threads):
        with closing(db.connect(settings.db_path)) as other:
            other.execute("PRAGMA busy_timeout=0")
            try:
                db.add_comment(
                    other,
                    report_id="prorate",
                    version=1,
                    author_id="carol000",
                    author_name="Carol",
                    body="Concurrent blocker",
                )
            except sqlite3.OperationalError as error:
                assert "locked" in str(error)
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
        with closing(db.connect(settings.db_path)) as other:
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
    assert 'hx-vals=\'{"blocking": "0", "version": "1"}\'' in page


def test_blocking_toggle_checks_version_inside_write_transaction(client, settings, monkeypatch):
    from contextlib import closing

    from verifair import db

    push(client)
    cid = _open_thread(client)
    original = db.set_blocking

    def interleave(conn, *args, **kwargs):
        with closing(db.connect(settings.db_path)) as other:
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
