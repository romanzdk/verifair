import importlib.metadata
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from verifair import db, web
from verifair.app import CSP as APP_CSP

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
    assert "No changes awaiting review" in filtered.text
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
    assert "Reload this page after your agent pushes." in page and 'class="waiting"' not in page
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
    resolved = client.post(f"/r/prorate/comments/{thread['id']}/resolve")
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
    rest = re.sub(r":root\s*\{[^}]*\}", "", css)
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
    assert "body.has-verdict .app-main { padding-bottom: 90px; }" in css
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


def test_web_posts_notify(settings, make_app, monkeypatch):
    from dataclasses import replace

    from fastapi.testclient import TestClient

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
        "Your access changed. Reload the page.",
    ]:
        assert copy in js


@pytest.fixture
def proxied(settings, make_app, monkeypatch):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from verifair import db

    shared = replace(settings, identity_header="X-Forwarded-Email", bootstrap_admin_id="boss")
    app = make_app(shared)
    conn = db.connect(shared.database_url)
    for id in ("alice@example.com", "bob@example.com"):
        db.add_user(conn, id=id, name=id.partition("@")[0], role="member")
    token, _ = db.create_token(conn, owner_id="alice@example.com", label="agent")
    conn.close()
    monkeypatch.setitem(H, "Authorization", f"Bearer {token}")
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


@pytest.fixture
def anonymous(shared_client):
    from fastapi.testclient import TestClient

    return TestClient(shared_client.app)


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
def test_shared_mode_without_identity_is_401(anonymous, method, path):
    r = getattr(anonymous, method)(path)
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
            shared_client.get("/", headers={"X-Forwarded-Email": who}),
            shared_client.post("/me", data={"name": "M"}, headers={"X-Forwarded-Email": who}),
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


def test_shared_mode_header_is_matched_case_insensitively(shared_client, as_user):
    page = shared_client.get(
        "/access/members", headers={"X-Forwarded-Email": " Admin@Example.COM "}
    )
    assert page.status_code == 200 and "Add member" in page.text
    as_user("bob@example.com")
    bob = shared_client.get("/", headers={"X-Forwarded-Email": "BOB@example.com"})
    assert bob.status_code == 200 and '<span class="me">bob</span>' in bob.text
    for value in ("Agent", "AGENT"):
        r = shared_client.get("/", headers={"X-Forwarded-Email": value})
        assert r.status_code == 401 and SIGN_IN_COPY in r.text


def test_shared_mode_duplicate_identity_header_is_401(anonymous):
    pair = [("X-Forwarded-Email", "admin@example.com"), ("X-Forwarded-Email", "eve@example.com")]
    for values in (pair, pair[:1] * 2):
        r = anonymous.get("/access", headers=values)
        assert r.status_code == 401 and SIGN_IN_COPY in r.text


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
    anonymous.get("/", headers=[("X-Forwarded-Email", "a"), ("X-Forwarded-Email", "b")])
    anonymous.get("/p/q", headers={"X-Forwarded-Email": "stranger@example.com"})
    anonymous.get("/access", headers={"X-Forwarded-Email": "gone@example.com"})
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("membership ")]
    assert lines == [
        "membership denied reason=missing-header path='/r/x'",
        "membership denied reason=duplicate-header path='/'",
        "membership denied reason=not-member path='/p/q'",
        "membership denied reason=inactive path='/access'",
    ]
    assert all(r.levelname == "INFO" for r in caplog.records if "membership" in r.getMessage())


def test_shared_mode_denial_log_escapes_control_characters(anonymous, caplog):
    caplog.set_level("INFO", logger="verifair")
    anonymous.get("/r/%1B%5B2K")
    assert "membership denied reason=missing-header path='/r/\\x1b[2K'" in caplog.text
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
    assert '<span class="me">Bob Builder</span>' in page and "Mallory" not in page
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
    assert '<span class="me">admin</span>' in admin


def test_shared_mode_browser_check_still_applies_to_members(anonymous):
    r = anonymous.post(
        "/me", data={"name": "M"}, headers={"X-Forwarded-Email": "admin@example.com"}
    )
    assert (r.status_code, r.json()) == (403, {"detail": "browser request required"})


def test_shared_mode_health_static_and_api_skip_the_membership_check(anonymous):
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
    push(client, id="prorate", agent="claude-code")
    page = client.get("/r/prorate").text
    assert "Written by <b>claude-code</b><" in page and "session</a>" not in page
    push(client, id="prorate")
    assert "Written by" not in client.get("/r/prorate").text


def test_changes_counts_unsigned(client):
    push(client)
    push(client, title="Other")
    sign(client)
    page = client.get("/changes").text
    assert page.count('aria-label="Unsigned · 1"') == 1
    assert page.count('aria-label="Awaiting review · 1"') == 1


AUTHOR_COPY = "Only alice can sign this report."


def test_identity_header_only_author_can_sign(proxied):
    push(proxied)
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
    push(proxied)
    proxied.cookies.set("vf_name", "Bob")
    r = proxied.post("/r/prorate/sign", data={"version": 1})
    assert r.status_code == 401 and api_report(proxied)["status"] == "unsigned"


NO_OWNER_COPY = "This report has no owner. Push it again with an agent token under a new id."


@pytest.mark.parametrize("author", [None, "alice@example.com"])
def test_identity_header_nobody_may_sign_a_report_without_owner(proxied, author):
    from verifair import db

    conn = db.connect(proxied.app.state.settings.database_url)
    db.push_report(
        conn, project="billing-service", title="Prorate", risk="high", body=SAMPLE, author=author
    )
    conn.close()
    for who in ("alice@example.com", "bob@example.com"):
        headers = {"X-Forwarded-Email": who}
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
    assert 'hx-vals=\'{"blocking": "0", "version": "1"}\'' in page


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
        "admin": {"X-Forwarded-Email": "admin@example.com"},
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
            (f"/r/r/comments/{cid}/resolve", None),
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
        "/r/r/verdict", data={"version": 1, "verdict": "approve"}, headers=people["dave"]
    )
    assert f'<p class="error" role="alert">{SHARED_BLOCKING_COPY}</p>' in r.text
    assert BLOCKING_COPY not in r.text


def test_shared_mode_blocked_approval_on_your_own_threads_says_resolve_yours(
    shared_client, moderation
):
    people, _, agent = moderation
    shared_client.post("/r/r/sign", data={"version": 1}, headers=people["owner"])
    verdict = {"version": 1, "verdict": "approve"}
    r = shared_client.post("/r/r/verdict", data=verdict, headers=people["carol"])
    assert f'<p class="error" role="alert">{SHARED_BLOCKING_COPY}</p>' in r.text
    shared_client.post(f"/r/r/comments/{agent}/resolve", headers=people["owner"])
    r = shared_client.post("/r/r/verdict", data=verdict, headers=people["carol"])
    assert f'<p class="error" role="alert">{OWN_BLOCKING_COPY}</p>' in r.text
    r = shared_client.post("/r/r/verdict", data=verdict, headers=people["dave"])
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
        headers=people["admin"],
    )
    assert r.status_code == 200 and "Mark blocking" in r.text
    r = shared_client.post(f"/r/r/comments/{human}/resolve", headers=people["carol"])
    assert r.status_code == 200 and 'class="thread resolved"' in r.text
    r = shared_client.post(f"/r/r/comments/{agent}/resolve", headers=people["owner"])
    assert r.status_code == 200 and 'class="thread resolved"' in r.text
    assert _moderation_state(shared_settings, human) == (1, "carol@example.com", 0)
    assert _moderation_state(shared_settings, agent) == (1, "owner@example.com", 1)

    r = shared_client.post(f"/r/r/comments/{agent}/reopen", headers=people["dave"])
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
    r = client.post(f"/r/prorate/comments/{cid}/resolve")
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
        assert 'href="/docs"' in page, path
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
        "Settings need a sign-in proxy. Set VERIFAIR_IDENTITY_HEADER to turn them on." in page
        and "README" not in page
    )
    assert "/access" not in page

    bob = shared_client.get("/", headers=as_user("bob@example.com", name="Bob Builder")).text
    assert '<span class="me">Bob Builder</span>' in bob and ">Member<" in bob
    assert 'data-copy-identity="bob@example.com"' in bob
    assert 'href="/access/tokens"' in bob and 'href="/access/members"' not in bob
    assert "sign-in proxy" not in bob
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
    dark = light | dict(
        re.findall(r"(--[\w-]+): (#[0-9a-f]{6})", css.split(":root {")[2].split("}")[0])
    )
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


def _rail_reviewers(page):
    return page.split("<h4>Reviewers</h4>")[1].split("<h4>Versions</h4>")[0]


def test_sidebar_lists_requested_reviewers_next_to_verdicts(client):
    push(client, reviewers=["bob00000", "carol000"])
    sign(client)
    rail = _rail_reviewers(client.get("/r/prorate").text)
    assert "Nobody yet." not in rail
    assert re.findall(r'<span class="rn">([^<]*)</span> · ([^<]*)</div>', rail) == [
        ("bob00000", "review requested"),
        ("carol000", "review requested"),
    ]
    client.cookies.set("vf_id", "bob00000")
    client.cookies.set("vf_name", "Bob")
    client.post("/r/prorate/verdict", data={"version": 1, "verdict": "approve"})
    rail = _rail_reviewers(client.get("/r/prorate").text)
    assert rail.count('class="rev"') == 2
    assert ">approved v1</span>" in rail and "Bob" in rail
    assert re.findall(r'<span class="rn">([^<]*)</span> · ([^<]*)</div>', rail) == [
        ("carol000", "review requested")
    ]
