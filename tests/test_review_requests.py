import re
import smtplib
from contextlib import closing
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from verifair import db

BODY = "## Intent\nCharge by day.\n\n## Judge this\nIs a **day** the right [unit](https://e.com)?"
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


class Outbox:
    def __init__(self):
        self.sent = []
        self.fail = False

    def __call__(self, host, port, timeout):
        outbox = self

        class Session:
            def __enter__(self):
                if outbox.fail:
                    raise ConnectionRefusedError
                return self

            def __exit__(self, *exc):
                pass

            def ehlo(self):
                pass

            def has_extn(self, name):
                return False

            def send_message(self, message, to_addrs):
                assert to_addrs == [message["To"]]
                outbox.sent.append(message)

        return Session()


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(smtplib, "SMTP", box)
    return box


@pytest.fixture
def mail(shared_settings, make_app, team, outbox):
    settings = replace(shared_settings, smtp_url="smtp://mail:1025", email_from="vf@example.com")
    app = make_app(settings)
    return TestClient(app, headers={"Sec-Fetch-Site": "same-origin"})


def sent(outbox):
    return [(m["To"], m["Subject"]) for m in outbox.sent]


def test_signing_emails_every_requested_reviewer_except_the_signer(mail, team, outbox):
    db.add_user(team, id="gone@example.com", name="gone", role="member")
    db.add_user(team, id="ops", name="ops", role="member")
    db.add_user(team, id="a@x.com,b@y.com", name="two", role="member")
    for who in ("bob@example.com", "gone@example.com", "ops", "a@x.com,b@y.com"):
        add(mail, who)
    add(mail, "carol@example.com", headers=ALICE)
    db.add_review_request(team, "r", "alice@example.com")
    db.update_user(team, "gone@example.com", active=False)
    assert outbox.sent == []
    assert mail.post("/r/r/sign", data={"version": 1}, headers=ALICE).status_code == 200
    assert sent(outbox) == [
        ("bob@example.com", 'Review requested: "T"'),
        ("carol@example.com", 'Review requested: "T"'),
    ]
    bob, carol = (m.get_content() for m in outbox.sent)
    assert bob == (
        'alice signed v1 of "T". You were asked to review it.\n\n'
        "Project: p · Risk: high\n\n"
        "Judge this:\nIs a day the right unit?\n\n"
        "Open it to comment, approve or request changes:\nhttp://localhost:8000/r/r\n\n"
        "— Sent by verifair. Replies to this mail are not read.\n"
    )
    assert carol.startswith('alice signed v1 of "T". You were asked to review it.\n')
    mail.post("/r/r/sign", data={"version": 1}, headers=ALICE)
    assert len(outbox.sent) == 2


def test_signing_names_who_asked_when_it_was_not_the_signer(mail, team, outbox):
    add(mail, "carol@example.com", headers=BOB)
    mail.post("/r/r/sign", data={"version": 1}, headers=ALICE)
    assert (
        outbox.sent[0]
        .get_content()
        .startswith('alice signed v1 of "T". You were asked to review it by bob.\n')
    )


def test_adding_a_reviewer_to_a_signed_version_emails_only_them(mail, team, outbox):
    add(mail, "bob@example.com")
    assert outbox.sent == []
    mail.post("/r/r/sign", data={"version": 1}, headers=ALICE)
    outbox.sent.clear()
    add(mail, "carol@example.com")
    add(mail, "carol@example.com")
    add(mail, "bob@example.com", headers=BOB)
    assert sent(outbox) == [("carol@example.com", 'Review requested: "T"')]
    assert (
        outbox.sent[0]
        .get_content()
        .startswith('bob asked you to review "T" (v1, signed by alice).\n')
    )


def test_one_email_per_reviewer_and_version(mail, team, outbox):
    add(mail, "carol@example.com")
    mail.post("/r/r/sign", data={"version": 1}, headers=ALICE)
    for _ in range(2):
        mail.post("/r/r/reviewers/carol%40example.com/remove", headers=BOB | HX)
        add(mail, "carol@example.com")
    assert sent(outbox) == [("carol@example.com", 'Review requested: "T"')]
    db.push_report(team, project="p", title="T", risk="high", body=BODY, id="r")
    mail.post("/r/r/sign", data={"version": 2}, headers=ALICE)
    assert len(outbox.sent) == 2 and "signed v2" in outbox.sent[1].get_content()


def test_requests_remember_who_asked(shared_client, team):
    add(shared_client, "carol@example.com")
    token, _ = db.create_token(team, owner_id="alice@example.com", label="agent")
    pushed = shared_client.post(
        "/api/reports",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "id": "r",
            "project": "p",
            "title": "T",
            "risk": "high",
            "body": BODY,
            "reviewers": ["carol@example.com", "admin@example.com", "alice@example.com"],
        },
    )
    assert pushed.status_code == 201
    assert db.review_requesters(team, "r") == {
        "carol@example.com": "bob@example.com",
        "admin@example.com": "alice@example.com",
    }


def test_a_push_makes_an_unsigned_version_so_it_sends_no_email(mail, team, outbox):
    mail.post("/r/r/sign", data={"version": 1}, headers=ALICE)
    token, _ = db.create_token(team, owner_id="alice@example.com", label="agent")
    pushed = mail.post(
        "/api/reports",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "id": "r",
            "project": "p",
            "title": "T",
            "risk": "high",
            "body": BODY,
            "reviewers": ["carol@example.com"],
        },
    )
    assert pushed.status_code == 201 and outbox.sent == []


def test_smtp_failure_logs_one_warning_and_the_request_succeeds(mail, team, outbox, caplog):
    add(mail, "bob@example.com")
    outbox.fail = True
    assert mail.post("/r/r/sign", data={"version": 1}, headers=ALICE).status_code == 200
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == ["email failed report=r recipients=1 error=ConnectionRefusedError code=-"]
    assert db.get_signature(team, "r", 1) is not None


def test_no_email_without_smtp(shared_client, team, outbox):
    add(shared_client, "bob@example.com")
    shared_client.post("/r/r/sign", data={"version": 1}, headers=ALICE)
    assert outbox.sent == []


def test_no_email_in_local_mode(settings, make_app, outbox):
    app = make_app(replace(settings, smtp_url="smtp://mail:1025", email_from="vf@example.com"))
    client = TestClient(app, headers={"Sec-Fetch-Site": "same-origin"})
    with closing(db.connect(settings.database_url)) as conn:
        db.push_report(conn, project="p", title="T", risk="low", body=BODY, id="r")
        db.add_review_request(conn, "r", "bob@example.com")
    client.cookies.set("vf_name", "Alice")
    assert client.post("/r/r/sign", data={"version": 1}).status_code == 200
    assert outbox.sent == []
