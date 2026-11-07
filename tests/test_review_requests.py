import re
import smtplib
from contextlib import closing
from dataclasses import replace

import pytest
from conftest import SessionClient, signed_in

from verifair import db

BODY = "## Intent\nCharge by day.\n\n## Judge this\nIs a **day** the right [unit](https://e.com)?"
ALICE = signed_in("alice@example.com")
BOB = signed_in("bob@example.com")
HX = {"HX-Request": "true"}
REFUSED = "Pick an active member who does not own this report."
KEPT = "carol already gave a verdict on this version, so the request stays."
ROW = re.compile(
    r'<span class="rn" title="([^"]*)">[^<]*</span>(?:<button[^>]*>Remove</button>)?</div>'
    r'\s*<p class="rev-state">([^<]+)</p>'
)


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
    assert ROW.findall(r.text) == [("carol", "review requested")]
    assert '<p class="dismiss" data-rev-status>Requested carol.</p>' in r.text
    assert 'hx-post="/r/r/reviewers/carol%40example.com/remove"' in r.text
    assert add(shared_client, "carol@example.com").status_code == 200
    assert db.requested_reviewers(team, "r") == ["carol@example.com"]
    added = "access action=add_reviewer actor=bob@example.com target=carol@example.com report=r"
    assert caplog.text.count(added) == 1
    r = shared_client.post("/r/r/reviewers/carol%40example.com/remove", headers=BOB | HX)
    assert r.status_code == 200 and ROW.findall(r.text) == []
    assert '<p class="dismiss" data-rev-status>Removed the request for carol.</p>' in r.text
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
    assert 'method="post" action="/r/r/reviewers"' in picker
    assert 'aria-label="Request a review from" required>' in picker
    assert '<option value="" disabled selected>Choose a member…</option>' in picker
    assert '<button class="btn sm" aria-label="Request review">Request</button>' in picker


def test_picker_sorts_by_name_and_tells_same_names_apart(shared_client, team):
    for id, name in (("c2@example.com", "Carol"), ("zed@example.com", "anna")):
        db.add_user(team, id=id, name=name, role="member")
    picker = rail(shared_client.get("/r/r", headers=BOB).text).split('<form class="addrev"')[1]
    assert re.findall(r'<option value="[^"]+">([^<]+)</option>', picker) == [
        "admin",
        "anna",
        "bob",
        "Carol (c2@example.com)",
        "carol (carol@example.com)",
    ]


def test_same_names_use_the_id_label_in_rows_status_and_picker(shared_client, team):
    db.add_user(team, id="c2@example.com", name="carol", role="member")
    r = add(shared_client, "c2@example.com")
    label = "carol (c2@example.com)"
    assert f'<p class="dismiss" data-rev-status>Requested {label}.</p>' in r.text
    assert ROW.findall(r.text) == [(label, "review requested")]
    assert f'aria-label="Remove review request for {label}"' in r.text
    picker = rail(r.text).split('<form class="addrev"')[1]
    assert ">carol (carol@example.com)</option>" in picker
    r = shared_client.post("/r/r/reviewers/c2%40example.com/remove", headers=BOB | HX)
    assert f"data-rev-status>Removed the request for {label}.</p>" in r.text


def test_removing_a_request_that_is_gone_says_so(shared_client, team):
    r = shared_client.post("/r/r/reviewers/carol%40example.com/remove", headers=BOB | HX)
    assert '<p class="dismiss" data-rev-status>carol had no review request.</p>' in r.text


def test_old_versions_say_where_to_change_requests(shared_client, team):
    db.push_report(team, project="p", title="T", risk="high", body=BODY, id="r")
    old = rail(shared_client.get("/r/r/v/1", headers=BOB).text)
    assert '<p class="dismiss">Open the latest version to change requests.</p>' in old
    assert "addrev" not in old
    assert "Open the latest" not in rail(shared_client.get("/r/r", headers=BOB).text)


@pytest.mark.parametrize("who", ["alice@example.com", "ghost@example.com", "gone@example.com", ""])
def test_owner_unknown_and_inactive_members_cannot_be_requested(shared_client, team, who):
    db.add_user(team, id="gone@example.com", name="gone", role="member")
    db.update_user(team, "gone@example.com", active=False)
    plain = shared_client.post("/r/r/reviewers", data={"reviewer_id": who}, headers=BOB)
    assert (plain.status_code, plain.json()) == (422, {"detail": REFUSED})
    page = add(shared_client, who)
    assert page.status_code == 200
    assert f'<p class="error" role="alert" tabindex="-1">{REFUSED}</p>' in page.text
    assert db.requested_reviewers(team, "r") == []


def test_a_reviewer_with_a_verdict_on_this_version_stays(shared_client, team):
    add(shared_client, "bob@example.com")
    add(shared_client, "carol@example.com")
    shared_client.post("/r/r/sign", data={"version": 1}, headers=ALICE)
    carol = signed_in("carol@example.com")
    shared_client.post("/r/r/verdict", data={"version": 1, "verdict": "approve"}, headers=carol)
    page = rail(shared_client.get("/r/r", headers=ALICE).text)
    assert "/reviewers/bob%40example.com/remove" in page
    assert "/reviewers/carol%40example.com/remove" not in page
    path = "/r/r/reviewers/carol%40example.com/remove"
    plain = shared_client.post(path, headers=ALICE)
    assert (plain.status_code, plain.json()) == (422, {"detail": KEPT})
    page = rail(shared_client.post(path, headers=ALICE | HX).text)
    carol_row = page.split('title="carol"')[1].split('class="rev-item"')[0]
    assert f'<p class="error" role="alert" tabindex="-1">{KEPT}</p>' in carol_row
    assert db.requested_reviewers(team, "r") == ["bob@example.com", "carol@example.com"]
    db.push_report(team, project="p", title="T", risk="high", body=BODY, id="r")
    assert "/reviewers/carol%40example.com/remove" in shared_client.get("/r/r", headers=BOB).text
    shared_client.post(path, headers=ALICE | HX)
    assert db.requested_reviewers(team, "r") == ["bob@example.com"]
    assert [v["reviewer_id"] for v in db.list_verdicts(team, "r")] == ["carol@example.com"]


def test_remove_path_ids_are_lower_cased(shared_client, team):
    add(shared_client, "carol@example.com")
    shared_client.post("/r/r/reviewers/Carol%40Example.COM/remove", headers=BOB | HX)
    assert db.requested_reviewers(team, "r") == []


def test_without_htmx_the_routes_redirect_to_the_report(shared_client, team):
    r = shared_client.post(
        "/r/r/reviewers",
        data={"reviewer_id": "carol@example.com"},
        headers=BOB,
        follow_redirects=False,
    )
    assert (r.status_code, r.headers["location"]) == (303, "/r/r")
    r = shared_client.post(
        "/r/r/reviewers/carol%40example.com/remove", headers=BOB, follow_redirects=False
    )
    assert (r.status_code, r.headers["location"]) == (303, "/r/r")
    assert db.requested_reviewers(team, "r") == []


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


def test_local_mode_can_remove_but_not_add(client, settings):
    with closing(db.connect(settings.database_url)) as conn:
        db.push_report(
            conn, project="p", title="T", risk="low", body=BODY, id="r", reviewers=["Bob"]
        )
        page = client.get("/r/r").text
        assert 'class="addrev"' not in page and 'hx-post="/r/r/reviewers/Bob/remove"' in page
        assert client.post("/r/r/reviewers", data={"reviewer_id": "x"}).status_code == 404
        r = client.post("/r/r/reviewers/Bob/remove", headers=HX)
        assert r.status_code == 200 and ROW.findall(r.text) == []
        assert "Removed the request for Bob." in r.text
        assert db.requested_reviewers(conn, "r") == []


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
    return SessionClient(app, headers={"Sec-Fetch-Site": "same-origin"})


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
        "Judge this (written by the agent, not checked by verifair):\nIs a day the right unit?\n\n"
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


def test_a_failed_email_is_sent_again_after_remove_and_add(mail, team, outbox):
    add(mail, "carol@example.com")
    outbox.fail = True
    mail.post("/r/r/sign", data={"version": 1}, headers=ALICE)
    assert outbox.sent == []
    outbox.fail = False
    mail.post("/r/r/reviewers/carol%40example.com/remove", headers=BOB | HX)
    add(mail, "carol@example.com")
    assert sent(outbox) == [("carol@example.com", 'Review requested: "T"')]


def test_the_signer_adding_a_reviewer_is_not_named_twice(mail, team, outbox):
    mail.post("/r/r/sign", data={"version": 1}, headers=ALICE)
    add(mail, "carol@example.com", headers=ALICE)
    assert outbox.sent[0].get_content().startswith('alice asked you to review "T" (v1).\n')


def test_no_email_without_smtp(shared_client, team, outbox):
    add(shared_client, "bob@example.com")
    shared_client.post("/r/r/sign", data={"version": 1}, headers=ALICE)
    assert outbox.sent == []


def test_no_email_in_local_mode(settings, make_app, outbox):
    app = make_app(replace(settings, smtp_url="smtp://mail:1025", email_from="vf@example.com"))
    client = SessionClient(app, headers={"Sec-Fetch-Site": "same-origin"})
    with closing(db.connect(settings.database_url)) as conn:
        db.push_report(conn, project="p", title="T", risk="low", body=BODY, id="r")
        db.add_review_request(conn, "r", "bob@example.com")
    client.cookies.set("vf_name", "Alice")
    assert client.post("/r/r/sign", data={"version": 1}).status_code == 200
    assert outbox.sent == []


def _requested_plan(conn, *, id="planned"):
    db.push_report(
        conn,
        id=id,
        project="p",
        title="Plan",
        risk="low",
        workflow="plan_first",
        body=None,
        body_supplied=False,
        plan="No headings required.",
        author_id="alice@example.com",
        reviewers=["bob@example.com"],
    )


def test_plan_review_request_removal_uses_both_targets(shared_client, team):
    _requested_plan(team)
    form = {"version": 1, "revision": 1, "target": "plan"}
    shared_client.post("/r/planned/sign", data=form, headers=ALICE)
    shared_client.post("/r/planned/verdict", data=form | {"verdict": "approve"}, headers=BOB)
    db.push_report(
        team, id="planned", project="p", title="Plan", risk="low", body=BODY, expected_version=1
    )
    page = shared_client.get("/r/planned?tab=implementation", headers=ALICE).text
    assert "Remove review request for bob" not in page
    result = shared_client.post(
        "/r/planned/reviewers/bob%40example.com/remove", headers=ALICE | HX, data={"target": "plan"}
    )
    assert result.status_code == 200 and "request stays" in result.text
    assert 'name="target" value="plan"' in result.text
    assert db.requested_reviewers(team, "planned") == ["bob@example.com"]
    assert (
        'name="target" value="plan"'
        in shared_client.post(
            "/r/planned/reviewers",
            headers=ALICE | HX,
            data={"target": "plan", "reviewer_id": "carol@example.com"},
        ).text
    )
    redirect = shared_client.post(
        "/r/planned/reviewers",
        headers=ALICE,
        data={"target": "plan", "reviewer_id": "carol@example.com"},
        follow_redirects=False,
    )
    assert redirect.headers["location"] == "/r/planned?tab=plan"


def test_plan_notifications_and_mail_dedupe(mail, team, outbox):
    _requested_plan(team)
    form = {"version": 1, "revision": 1, "target": "plan"}
    assert mail.post("/r/planned/sign", data=form, headers=ALICE).status_code == 200
    assert len(outbox.sent) == 1 and "plan revision 1" in outbox.sent[0].get_content()
    assert "Judge this" not in outbox.sent[0].get_content()
    assert "?tab=plan" in outbox.sent[0].get_content()
    mail.post("/r/planned/sign", data=form, headers=ALICE)
    assert len(outbox.sent) == 1
    mail.post("/r/planned/verdict", data=form | {"verdict": "approve"}, headers=BOB)
    mail.post("/r/planned/reviewers", headers=ALICE, data={"reviewer_id": "carol@example.com"})
    assert len(outbox.sent) == 1
    assert db.requested_reviewers(team, "planned") == ["bob@example.com", "carol@example.com"]
    db.push_report(
        team, id="planned", project="p", title="Plan", risk="low", body=BODY, expected_version=1
    )
    mail.post(
        "/r/planned/sign",
        data={"version": 2, "revision": 2, "target": "implementation"},
        headers=ALICE,
    )
    assert len(outbox.sent) == 3
    assert all(
        "implementation v2" in m.get_content() and "?tab=implementation" in m.get_content()
        for m in outbox.sent[1:]
    )
    assert ("planned", "bob@example.com", "plan", 1) in mail.app.state.mailed
    assert ("planned", "bob@example.com", "implementation", 2) in mail.app.state.mailed
    db.push_report(
        team,
        id="planned",
        project="p",
        title="Plan",
        risk="low",
        body=None,
        body_supplied=False,
        plan="A new plan",
        expected_version=2,
    )
    mail.post(
        "/r/planned/sign", data={"version": 3, "revision": 3, "target": "plan"}, headers=ALICE
    )
    assert len(outbox.sent) == 5 and "plan revision 3" in outbox.sent[-1].get_content()


def test_plan_failed_mail_releases_only_captured_target_key(mail, team, outbox):
    import asyncio

    from verifair import web

    mail.app.state.mailed.update(
        {
            ("planned", "bob@example.com", "plan", 1),
            ("planned", "bob@example.com", "implementation", 1),
        }
    )
    outbox.fail = True
    asyncio.run(
        web._send_mail(
            mail.app, "planned", 1, "subject", {"bob@example.com": "text"}, target="plan"
        )
    )
    assert mail.app.state.mailed == {("planned", "bob@example.com", "implementation", 1)}


def test_adding_a_member_emails_them_how_to_start(mail, outbox):
    form = {"id": "Dana@Example.com", "name": "Dana", "role": "member"}
    r = mail.post("/access/users", data=form, headers=signed_in("admin@example.com"))
    assert r.status_code == 200
    [message] = outbox.sent
    assert (message["To"], message["Subject"]) == ("dana@example.com", "You were added to verifair")
    text = message.get_content()
    assert "admin added you to verifair" in text
    assert "http://localhost:8000/access/tokens" in text


def test_adding_a_member_without_mail_sends_nothing(shared_client, outbox):
    shared_client.post("/access/users", data={"id": "dana@example.com", "name": "Dana"})
    assert outbox.sent == []
