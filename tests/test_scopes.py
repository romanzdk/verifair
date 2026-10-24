import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from verifair import db
from verifair.app import create_app

TOKEN = {"Authorization": "Bearer t"}
AGENTS = {}
ALICE = {"X-Forwarded-Email": "alice@example.com"}
BOB = {"X-Forwarded-Email": "bob@example.com"}


@pytest.fixture
def team(settings, monkeypatch):
    shared = replace(settings, identity_header="X-Forwarded-Email", bootstrap_admin_id="boss")
    app = create_app(shared)
    conn = db.connect(shared.db_path)
    for id in ("alice@example.com", "bob@example.com", "carol@example.com"):
        db.add_user(conn, id=id, name=id.partition("@")[0], role="member")
        token, _ = db.create_token(conn, owner_id=id, label="agent")
        monkeypatch.setitem(AGENTS, id, {"Authorization": f"Bearer {token}"})
    conn.close()
    monkeypatch.setitem(TOKEN, "Authorization", AGENTS["alice@example.com"]["Authorization"])
    return TestClient(app, headers={"Sec-Fetch-Site": "same-origin"})


def main(page):
    return page.split('<div class="app-main">')[1]


def publish(client, id="owned", by=None, **overrides):
    return client.post(
        "/api/reports",
        headers=AGENTS[by] if by else TOKEN,
        json={
            "id": id,
            "project": "billing",
            "title": id,
            "risk": "low",
            "body": "## Intent\nReview the evidence.",
        }
        | overrides,
    )


def report(client, id="owned"):
    return client.get(f"/api/reports/{id}", headers=TOKEN).json()


def seed(team):
    bob = "bob@example.com"
    assert publish(team, author_id="alice@example.com", reviewers=[bob]).status_code == 201
    assert publish(team, "requested", by=bob, reviewers=["alice@example.com"]).status_code == 201
    assert publish(team, "elsewhere", by=bob, project="other").status_code == 201


def test_local_publish_preserves_and_explicitly_replaces_ownership_and_requests(client):
    response = publish(
        client,
        author="Alice",
        author_id="alice@example.com",
        reviewers=["bob@example.com", "bob@example.com"],
    )
    assert response.status_code == 201
    first = report(client)
    assert (first["author_id"], first["reviewers"]) == (
        "alice@example.com",
        ["bob@example.com"],
    )
    assert publish(client).json()["version"] == 2
    revised = report(client)
    assert (revised["author"], revised["author_id"], revised["reviewers"]) == (
        "Alice",
        "alice@example.com",
        ["bob@example.com"],
    )
    publish(client, author="Bob", author_id="bob@example.com", reviewers=["alice@example.com"])
    reassigned = report(client)
    assert (reassigned["author_id"], reassigned["reviewers"]) == (
        "bob@example.com",
        ["alice@example.com"],
    )
    publish(client, reviewers=[])
    assert report(client)["reviewers"] == []


@pytest.mark.parametrize(
    "fields",
    [
        {"author_id": "   "},
        {"author_id": "agent"},
        {"author_id": "a" * 257},
        {"reviewers": [""]},
        {"reviewers": ["agent"]},
        {"reviewers": ["b" * 257]},
        {"reviewers": [f"user-{n}" for n in range(51)]},
    ],
)
def test_invalid_assignment_identities_do_not_create_reports(team, fields):
    assert publish(team, **fields).status_code == 422
    assert team.get("/api/reports/owned", headers=TOKEN).status_code == 404


def test_all_is_shared_and_personal_scopes_filter_rows_counts_and_links(team):
    seed(team)
    for identity in (ALICE, BOB):
        everyone = team.get("/changes", headers=identity).text
        for id in ("owned", "requested", "elsewhere"):
            assert f'href="/r/{id}"' in everyone
        all_rows = team.get("/p/billing", headers=identity).text
        assert 'href="/r/owned"' in all_rows and 'href="/r/requested"' in all_rows
        assert 'href="/r/elsewhere"' not in all_rows

    mine = team.get("/changes?scope=author", headers=ALICE).text
    assert 'href="/r/owned"' in mine and 'href="/r/requested"' not in mine
    assert 'href="/r/elsewhere"' not in mine
    assert 'aria-label="Unsigned · 1"' in mine and 'aria-label="All · 1"' in mine
    assert 'href="?tab=unsigned&amp;scope=author"' in mine
    rows = team.get("/p/billing?scope=author", headers=ALICE).text
    assert 'href="/r/owned"' in rows and 'href="/r/requested"' not in rows


def test_my_reviews_includes_requested_work_before_activity_and_after_completion(team):
    seed(team)
    assert team.get("/api/reports/requested/comments", headers=TOKEN).json() == {"threads": []}
    before = team.get("/changes?scope=reviewer", headers=ALICE).text
    assert 'href="/r/requested"' in before and 'href="/r/owned"' not in before
    assert team.post("/r/requested/sign", headers=BOB, data={"version": 1}).status_code == 200
    assert (
        team.post(
            "/r/requested/verdict", headers=ALICE, data={"version": 1, "verdict": "approve"}
        ).status_code
        == 200
    )
    completed = team.get("/changes?scope=reviewer&tab=approved", headers=ALICE).text
    assert 'href="/r/requested"' in completed and 'href="/r/owned"' not in completed
    assert publish(team, "requested", by="bob@example.com").json()["version"] == 2
    draft = team.get("/changes?scope=reviewer&tab=unsigned", headers=ALICE).text
    assert 'href="/r/requested"' in draft
    assert report(team, "requested")["reviewers"] == ["alice@example.com"]


def test_commenting_does_not_assign_a_review_or_restrict_shared_access(team):
    seed(team)
    team.post("/r/owned/comments", headers=ALICE, data={"version": 1, "body": "A note"})
    reviews = team.get("/changes?scope=reviewer", headers=ALICE).text
    assert 'href="/r/owned"' not in reviews
    assert team.get("/r/owned", headers=BOB).status_code == 200
    assert team.get("/r/requested", headers=ALICE).status_code == 200


def test_scope_links_keep_status_and_empty_status_recovery_keeps_scope(team):
    seed(team)
    page = team.get("/changes?scope=author&tab=approved", headers=ALICE).text
    assert 'href="?tab=unsigned&amp;scope=author"' in page
    assert 'href="?tab=all&amp;scope=author"' in page
    assert "No approved changes yet" in page
    fallback = team.get("/changes?scope=bogus&tab=bogus", headers=ALICE).text
    assert 'href="/r/owned"' in fallback and 'href="/r/requested"' in fallback
    assert 'href="/r/elsewhere"' in fallback
    assert "?scope=" not in fallback.split('<nav class="tabs"')[1].split("</nav>")[0]


def test_old_scope_urls_redirect_to_changes(team):
    author = team.get("/?scope=author", headers=ALICE, follow_redirects=False)
    assert (author.status_code, author.headers["location"]) == (303, "/changes?scope=author")
    bogus = team.get("/?scope=bogus", headers=ALICE, follow_redirects=False)
    assert (bogus.status_code, bogus.headers["location"]) == (303, "/changes")


def test_no_matches_is_a_useful_empty_state_and_not_a_missing_project(team):
    seed(team)
    empty = team.get("/p/other?scope=author", headers=ALICE)
    assert empty.status_code == 200 and "No changes owned by you" in empty.text
    assert 'href="?scope=all&amp;tab=all"' in empty.text
    missing = team.get("/p/missing?scope=author", headers=ALICE)
    assert missing.status_code == 404
    reviews = team.get(
        "/changes?scope=reviewer", headers={"X-Forwarded-Email": "carol@example.com"}
    )
    assert "No review requests for you yet" in reviews.text
    assert 'href="?scope=all&amp;tab=all"' in reviews.text


def test_identity_is_stable_across_browsers_and_not_matched_by_display_name(team):
    seed(team)
    conn = db.connect(team.app.state.settings.db_path)
    db.push_report(
        conn,
        id="same-name",
        project="other",
        title="same-name",
        risk="low",
        body="## Intent\nx",
        author="Alice",
        author_id="carol@example.com",
    )
    conn.close()
    team.cookies.set("vf_id", "bob00000")
    team.cookies.set("vf_name", "Bob")
    first = team.get("/changes?scope=author", headers=ALICE).text
    team.cookies.set("vf_id", "carol000")
    team.cookies.set("vf_name", "Alice")
    second = team.get("/changes?scope=author", headers=ALICE).text
    assert 'href="/r/owned"' in first and 'href="/r/owned"' in second
    assert 'href="/p/other' not in main(first) and 'href="/p/other' not in main(second)


def test_canonical_owner_can_sign_with_a_separate_display_name(team):
    seed(team)
    refused = team.post("/r/owned/sign", headers=BOB, data={"version": 1})
    assert refused.status_code == 403
    assert team.post("/r/owned/sign", headers=ALICE, data={"version": 1}).status_code == 200
    assert report(team)["status"] == "awaiting_review"


def test_browser_signing_claims_unowned_changes_and_identity_is_available_to_copy(client):
    publish(client)
    client.cookies.set("vf_id", "alice000")
    client.cookies.set("vf_name", "Alice")
    client.post("/r/owned/sign", data={"version": 1})
    assert report(client)["author_id"] == "alice000"
    publish(client)
    mine = client.get("/changes?scope=author").text
    assert 'href="/r/owned"' in mine
    assert 'data-copy-identity="alice000"' in mine
    client.cookies.set("vf_id", "bob00000")
    client.cookies.set("vf_name", "Alice")
    assert 'href="/r/owned"' not in client.get("/changes?scope=author").text


def test_local_my_changes_matches_the_legacy_author(client):
    conn = db.connect(client.app.state.settings.db_path)
    db.push_report(conn, project="p", title="Legacy", risk="low", body="a", author="alice000")
    db.push_report(conn, project="p", title="Owned", risk="low", body="b", author_id="alice000")
    db.push_report(conn, project="p", title="Other", risk="low", body="c", author="bob00000")
    conn.close()
    client.cookies.set("vf_id", "alice000")
    mine = main(client.get("/changes?scope=author&tab=all").text)
    assert 'href="/r/legacy"' in mine and 'href="/r/owned"' in mine
    assert 'href="/r/other"' not in mine


def test_schema_upgrade_recovers_owner_from_latest_signature_only(settings):
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    db.push_report(conn, project="p", title="Signed", risk="low", body="v1")
    db.sign_version(conn, report_id="signed", version=1, signer_id="alice000", signer_name="Alice")
    db.push_report(conn, project="p", title="Signed", risk="low", body="v2", id="signed")
    db.sign_version(conn, report_id="signed", version=2, signer_id="bob00000", signer_name="Bob")
    db.push_report(conn, project="p", title="Draft", risk="low", body="v1", author="Alice")
    conn.execute("UPDATE reports SET author_id=NULL")
    db.init_schema(conn)
    db.init_schema(conn)
    assert db.get_report(conn, "signed")["author_id"] == "bob00000"
    assert db.get_report(conn, "draft")["author_id"] is None
    conn.close()


def test_shared_my_changes_ignores_a_legacy_author_equal_to_the_user(team):
    conn = db.connect(team.app.state.settings.db_path)
    db.push_report(
        conn,
        id="legacy",
        project="billing",
        title="legacy",
        risk="low",
        body="## Intent\nx",
        author="alice@example.com",
    )
    conn.close()
    assert 'href="/r/legacy"' in team.get("/p/billing", headers=ALICE).text
    assert 'href="/r/legacy"' not in team.get("/changes?scope=author", headers=ALICE).text


GROUPS = {"Needs your review", "Needs your signature", "Returned to you", "Waiting on others"}


def sections(page):
    body, _, waiting = main(page).partition('<details class="grp"')
    found = {"Waiting on others": waiting} if waiting else {}
    for part in body.split('<h2 class="grp-h"')[1:]:
        found[part.split("</svg>", 1)[1].split("<", 1)[0]] = part
    assert set(found) <= GROUPS
    assert len(found) == main(page).count('class="grp"')
    return found


def inbox(client, who):
    return sections(client.get("/", headers=who).text)


def test_inbox_follows_whose_turn_it_is(team):
    bob = "bob@example.com"
    assert publish(team, "r1", by=bob, reviewers=["alice@example.com"]).status_code == 201
    assert "Needs your review" not in inbox(team, ALICE)
    assert 'href="/r/r1"' in inbox(team, BOB)["Needs your signature"]

    assert team.post("/r/r1/sign", headers=BOB, data={"version": 1}).status_code == 200
    review = inbox(team, ALICE)["Needs your review"]
    assert 'href="/r/r1"' in review and "waiting" in review
    waiting = inbox(team, BOB)["Waiting on others"]
    assert 'href="/r/r1"' in waiting and "Waiting on others" in waiting

    verdict = {"version": 1, "verdict": "request_changes", "summary": "Add a test."}
    assert team.post("/r/r1/verdict", headers=ALICE, data=verdict).status_code == 200
    assert 'href="/r/r1"' in inbox(team, BOB)["Returned to you"]
    assert 'href="/r/r1"' not in main(team.get("/", headers=ALICE).text)

    assert publish(team, "r1", by=bob).json()["version"] == 2
    assert 'href="/r/r1"' in inbox(team, BOB)["Needs your signature"]
    assert 'href="/r/r1"' not in main(team.get("/", headers=ALICE).text)
    assert team.post("/r/r1/sign", headers=BOB, data={"version": 2}).status_code == 200
    assert 'href="/r/r1"' in inbox(team, ALICE)["Needs your review"]


def test_inbox_orders_high_risk_then_oldest(team):
    bob = "bob@example.com"
    signed = {
        "low-old": ("low", "2026-10-01T00:00:00+00:00"),
        "high-new": ("high", "2026-10-03T00:00:00+00:00"),
        "low-new": ("low", "2026-10-02T00:00:00+00:00"),
    }
    for id, (risk, _) in signed.items():
        assert (
            publish(team, id, by=bob, risk=risk, reviewers=["alice@example.com"]).status_code == 201
        )
        assert team.post(f"/r/{id}/sign", headers=BOB, data={"version": 1}).status_code == 200
    conn = db.connect(team.app.state.settings.db_path)
    with conn:
        for id, (_, at) in signed.items():
            conn.execute("UPDATE signatures SET created_at=? WHERE report_id=?", (at, id))
    conn.close()
    review = inbox(team, ALICE)["Needs your review"]
    order = [review.index(f'href="/r/{id}"') for id in ("high-new", "low-old", "low-new")]
    assert order == sorted(order)


def test_inbox_self_requested_author_is_not_asked_to_review(team):
    alice = "alice@example.com"
    assert publish(team, "self", by=alice, reviewers=[alice]).status_code == 201
    assert team.post("/r/self/sign", headers=ALICE, data={"version": 1}).status_code == 200
    page = team.get("/", headers=ALICE).text
    groups = sections(page)
    assert "Needs your review" not in groups
    assert 'href="/r/self"' in groups["Waiting on others"]
    assert "You're all caught up" in page


def test_inbox_local_mode_signer_and_unowned_drafts(client):
    def as_user(id):
        client.cookies.clear()
        client.cookies.set("vf_id", id)
        client.cookies.set("vf_name", id[:3])
        return sections(client.get("/").text)

    assert publish(client, author_id="alice000", reviewers=["bob00000"]).status_code == 201
    assert publish(client, "unowned").status_code == 201
    assert publish(client, "asked", reviewers=["bob00000"]).status_code == 201
    for user in ("bob00000", "carol000"):
        assert 'href="/r/unowned"' in as_user(user)["Needs your signature"]
    assert 'href="/r/asked"' not in as_user("bob00000")["Needs your signature"]
    assert 'href="/r/asked"' in as_user("carol000")["Needs your signature"]

    as_user("bob00000")
    assert client.post("/r/owned/sign", data={"version": 1}).status_code == 200
    assert "Needs your review" not in as_user("bob00000")
    assert 'href="/r/owned"' in as_user("alice000")["Waiting on others"]

    as_user("carol000")
    assert client.post("/r/unowned/sign", data={"version": 1}).status_code == 200
    assert 'href="/r/unowned"' in as_user("carol000")["Waiting on others"]
    assert "Needs your signature" not in as_user("bob00000")


def nav_link(page, href):
    return re.search(rf'<a href="{re.escape(href)}"[^>]*>.*?</a>', page).group(0)


def test_sidebar_counts(team):
    bob = "bob@example.com"
    assert publish(team, "r1", by=bob, reviewers=["alice@example.com"]).status_code == 201
    assert publish(team, "r2", by=bob, project="other").status_code == 201
    assert team.post("/r/r1/sign", headers=BOB, data={"version": 1}).status_code == 200
    page = team.get("/", headers=ALICE).text
    assert 'class="nav-n acc">1<' in nav_link(page, "/")
    assert ">1<" in nav_link(page, "/p/billing")
    assert "nav-n" not in nav_link(page, "/p/other")
    assert 'class="nav-n acc">1<' in nav_link(team.get("/r/r1", headers=ALICE).text, "/")
    assert 'class="nav-n acc">1<' in nav_link(team.get("/", headers=BOB).text, "/")
    carol = {"X-Forwarded-Email": "carol@example.com"}
    assert "nav-n" not in nav_link(team.get("/", headers=carol).text, "/")
