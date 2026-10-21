from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from verifair import db
from verifair.app import create_app

TOKEN = {"Authorization": "Bearer t"}
ALICE = {"X-Forwarded-Email": "alice@example.com"}
BOB = {"X-Forwarded-Email": "bob@example.com"}


@pytest.fixture
def team(settings):
    shared = replace(settings, identity_header="X-Forwarded-Email", bootstrap_admin_id="boss")
    app = create_app(shared)
    conn = db.connect(shared.db_path)
    for id in ("alice@example.com", "bob@example.com", "carol@example.com"):
        db.add_user(conn, id=id, name=id.partition("@")[0], role="member")
    conn.close()
    return TestClient(app, headers={"Sec-Fetch-Site": "same-origin"})


def publish(client, id="owned", **overrides):
    return client.post(
        "/api/reports",
        headers=TOKEN,
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
    assert (
        publish(
            team, author="Alice", author_id="alice@example.com", reviewers=["bob@example.com"]
        ).status_code
        == 201
    )
    assert (
        publish(
            team,
            "requested",
            author="Bob",
            author_id="bob@example.com",
            reviewers=["alice@example.com"],
        ).status_code
        == 201
    )
    assert (
        publish(team, "elsewhere", project="other", author_id="bob@example.com").status_code == 201
    )


def test_publish_preserves_and_explicitly_replaces_ownership_and_requests(team):
    response = publish(
        team,
        author="Alice",
        author_id="alice@example.com",
        reviewers=["bob@example.com", "bob@example.com"],
    )
    assert response.status_code == 201
    first = report(team)
    assert (first["author_id"], first["reviewers"]) == (
        "alice@example.com",
        ["bob@example.com"],
    )
    assert publish(team).json()["version"] == 2
    revised = report(team)
    assert (revised["author"], revised["author_id"], revised["reviewers"]) == (
        "Alice",
        "alice@example.com",
        ["bob@example.com"],
    )
    publish(team, author="Bob", author_id="bob@example.com", reviewers=["alice@example.com"])
    reassigned = report(team)
    assert (reassigned["author_id"], reassigned["reviewers"]) == (
        "bob@example.com",
        ["alice@example.com"],
    )
    publish(team, reviewers=[])
    assert report(team)["reviewers"] == []


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


def test_all_is_shared_and_personal_scopes_filter_projects_counts_and_previews(team):
    seed(team)
    for identity in (ALICE, BOB):
        everyone = team.get("/", headers=identity).text
        assert 'href="/p/billing"' in everyone and 'href="/p/other"' in everyone
        all_rows = team.get("/p/billing", headers=identity).text
        assert 'href="/r/owned"' in all_rows and 'href="/r/requested"' in all_rows

    mine = team.get("/?scope=author", headers=ALICE).text
    assert 'href="/p/billing?scope=author"' in mine
    assert 'href="/p/other' not in mine
    assert 'href="/r/owned"' in mine and 'href="/r/requested"' not in mine
    assert 'aria-label="Unsigned: 1 change"' in mine
    assert 'href="/p/billing?tab=unsigned&amp;scope=author"' in mine
    rows = team.get("/p/billing?scope=author", headers=ALICE).text
    assert 'href="/r/owned"' in rows and 'href="/r/requested"' not in rows


def test_my_reviews_includes_requested_work_before_activity_and_after_completion(team):
    seed(team)
    assert team.get("/api/reports/requested/comments", headers=TOKEN).json() == {"threads": []}
    before = team.get("/p/billing?scope=reviewer", headers=ALICE).text
    assert 'href="/r/requested"' in before and 'href="/r/owned"' not in before
    assert team.post("/r/requested/sign", headers=BOB, data={"version": 1}).status_code == 200
    assert (
        team.post(
            "/r/requested/verdict", headers=ALICE, data={"version": 1, "verdict": "approve"}
        ).status_code
        == 200
    )
    completed = team.get("/p/billing?scope=reviewer&tab=approved", headers=ALICE).text
    assert 'href="/r/requested"' in completed and 'href="/r/owned"' not in completed
    assert publish(team, "requested").json()["version"] == 2
    draft = team.get("/p/billing?scope=reviewer&tab=unsigned", headers=ALICE).text
    assert 'href="/r/requested"' in draft
    assert report(team, "requested")["reviewers"] == ["alice@example.com"]


def test_commenting_does_not_assign_a_review_or_restrict_shared_access(team):
    seed(team)
    team.post("/r/owned/comments", headers=ALICE, data={"version": 1, "body": "A note"})
    reviews = team.get("/p/billing?scope=reviewer", headers=ALICE).text
    assert 'href="/r/owned"' not in reviews
    assert team.get("/r/owned", headers=BOB).status_code == 200
    assert team.get("/r/requested", headers=ALICE).status_code == 200


def test_scope_links_keep_status_and_empty_status_recovery_keeps_scope(team):
    seed(team)
    page = team.get("/p/billing?scope=author&tab=approved", headers=ALICE).text
    assert 'href="?scope=reviewer&amp;tab=approved"' in page
    assert 'href="?tab=unsigned&amp;scope=author"' in page
    assert 'href="?tab=all&amp;scope=author"' in page
    assert 'href="/?scope=author"' in page
    assert "No approved changes yet" in page
    fallback = team.get("/p/billing?scope=bogus&tab=bogus", headers=ALICE).text
    assert 'href="/r/owned"' in fallback and 'href="/r/requested"' in fallback


def test_no_matches_is_a_useful_empty_state_and_not_a_missing_project(team):
    seed(team)
    empty = team.get("/p/other?scope=author", headers=ALICE)
    assert empty.status_code == 200 and "No changes owned by you" in empty.text
    assert 'href="?scope=all&amp;tab=all"' in empty.text
    missing = team.get("/p/missing?scope=author", headers=ALICE)
    assert missing.status_code == 404
    reviews = team.get("/?scope=reviewer", headers={"X-Forwarded-Email": "carol@example.com"})
    assert "No review requests for you yet" in reviews.text
    assert 'href="/"' in reviews.text


def test_identity_is_stable_across_browsers_and_not_matched_by_display_name(team):
    seed(team)
    publish(team, "same-name", project="other", author="Alice", author_id="carol@example.com")
    team.cookies.set("vf_id", "bob00000")
    team.cookies.set("vf_name", "Bob")
    first = team.get("/?scope=author", headers=ALICE).text
    team.cookies.set("vf_id", "carol000")
    team.cookies.set("vf_name", "Alice")
    second = team.get("/?scope=author", headers=ALICE).text
    assert 'href="/r/owned"' in first and 'href="/r/owned"' in second
    assert 'href="/p/other' not in first and 'href="/p/other' not in second


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
    mine = client.get("/?scope=author").text
    assert 'href="/r/owned"' in mine
    assert 'data-copy-identity="alice000"' in mine
    client.cookies.set("vf_id", "bob00000")
    client.cookies.set("vf_name", "Alice")
    assert 'href="/r/owned"' not in client.get("/?scope=author").text


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
