import re
from contextlib import closing

import pytest

from verifair import db

BODY = "## Intent\nCharge by day.\n\n## Judge this\nIs a day the right unit?"
ALICE = {"X-Forwarded-Email": "alice@example.com"}
BOB = {"X-Forwarded-Email": "bob@example.com"}
HX = {"HX-Request": "true"}
REFUSED = "Pick an active member who does not own this report."


@pytest.fixture
def conn(shared_client, shared_settings):
    with closing(db.connect(shared_settings.database_url)) as conn:
        yield conn


@pytest.fixture
def team(conn, as_user):
    for id in ("alice@example.com", "bob@example.com", "carol@example.com"):
        as_user(id)
    db.push_report(
        conn, project="p", title="T", risk="high", body=BODY, id="r", owner_id="alice@example.com"
    )
    return conn


def rail(html):
    return html.split("<h4>Reviewers</h4>")[1].split("<h4>Versions</h4>")[0]


def add(client, who, headers=BOB, id="r"):
    return client.post(f"/r/{id}/reviewers", data={"reviewer_id": who}, headers=headers | HX)


def test_member_requests_and_removes_a_review(shared_client, team, caplog):
    caplog.set_level("INFO", logger="verifair")
    r = add(shared_client, "Carol@example.com")
    assert r.status_code == 200
    assert db.requested_reviewers(team, "r") == ["carol@example.com"]
    assert r.text.startswith('<aside class="side" id="vf_sidebar">')
    assert 'id="vf_nav" popover aria-label="Main" hx-swap-oob="true"' in r.text
    assert re.search(r'<span class="rn">carol</span> · review requested<button', r.text)
    assert 'hx-post="/r/r/reviewers/carol%40example.com/remove"' in r.text
    assert add(shared_client, "carol@example.com").status_code == 200
    assert db.requested_reviewers(team, "r") == ["carol@example.com"]
    added = "access action=add_reviewer actor=bob@example.com target=carol@example.com report=r"
    assert caplog.text.count(added) == 1
    r = shared_client.post("/r/r/reviewers/carol%40example.com/remove", headers=BOB | HX)
    assert r.status_code == 200 and "carol" not in rail(r.text).split("<form")[0]
    assert db.requested_reviewers(team, "r") == []
    assert (
        "access action=remove_reviewer actor=bob@example.com target=carol@example.com report=r"
    ) in caplog.text


def test_picker_lists_active_members_who_are_not_the_owner_or_requested(shared_client, team):
    db.add_user(team, id="gone@example.com", name="gone", role="member")
    db.update_user(team, "gone@example.com", active=False)
    add(shared_client, "carol@example.com")
    page = rail(shared_client.get("/r/r", headers=BOB).text)
    picker = page.split('<form class="addrev"')[1]
    assert re.findall(r'<option value="([^"]+)">', picker) == [
        "admin@example.com",
        "bob@example.com",
    ]
    assert 'hx-post="/r/r/reviewers"' in picker and 'name="reviewer_id"' in picker


@pytest.mark.parametrize("who", ["alice@example.com", "ghost@example.com", "gone@example.com", ""])
def test_owner_unknown_and_inactive_members_cannot_be_requested(shared_client, team, who):
    db.add_user(team, id="gone@example.com", name="gone", role="member")
    db.update_user(team, "gone@example.com", active=False)
    plain = shared_client.post("/r/r/reviewers", data={"reviewer_id": who}, headers=BOB)
    assert (plain.status_code, plain.json()) == (422, {"detail": REFUSED})
    page = add(shared_client, who)
    assert page.status_code == 200 and f'<p class="error" role="alert">{REFUSED}</p>' in page.text
    assert db.requested_reviewers(team, "r") == []


def test_remove_keeps_verdicts_and_hides_the_button_once_judged(shared_client, team):
    add(shared_client, "bob@example.com")
    add(shared_client, "carol@example.com")
    shared_client.post("/r/r/sign", data={"version": 1}, headers=ALICE)
    shared_client.post("/r/r/verdict", data={"version": 1, "verdict": "approve"}, headers=BOB)
    page = rail(shared_client.get("/r/r", headers=ALICE).text)
    assert "/reviewers/carol%40example.com/remove" in page
    assert "/reviewers/bob%40example.com/remove" not in page
    shared_client.post("/r/r/reviewers/bob%40example.com/remove", headers=ALICE | HX)
    assert db.requested_reviewers(team, "r") == ["carol@example.com"]
    assert [v["reviewer_id"] for v in db.list_verdicts(team, "r")] == ["bob@example.com"]


def test_ids_with_special_characters_round_trip(shared_client, team):
    for id in ("jane+ci@example.com", "team/ops"):
        db.add_user(team, id=id, name=id, role="member")
        assert add(shared_client, id).status_code == 200
    page = shared_client.get("/r/r", headers=BOB).text
    for path in ("jane%2Bci%40example.com", "team%2Fops"):
        assert f'hx-post="/r/r/reviewers/{path}/remove"' in page
        shared_client.post(f"/r/r/reviewers/{path}/remove", headers=BOB | HX)
    assert db.requested_reviewers(team, "r") == []


def test_reviews_can_be_requested_on_unowned_reports(shared_client, team):
    db.push_report(team, project="p", title="Legacy", risk="low", body=BODY, id="legacy")
    assert add(shared_client, "carol@example.com", id="legacy").status_code == 200
    assert db.requested_reviewers(team, "legacy") == ["carol@example.com"]


def test_local_mode_has_no_picker_and_refuses_the_routes(client, settings):
    with closing(db.connect(settings.database_url)) as conn:
        db.push_report(conn, project="p", title="T", risk="low", body=BODY, id="r")
    assert 'class="addrev"' not in client.get("/r/r").text
    assert client.post("/r/r/reviewers", data={"reviewer_id": "bob"}).status_code == 404
    assert client.post("/r/r/reviewers/bob/remove").status_code == 404
