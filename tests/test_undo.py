from conftest import signed_in
from test_web import api_report, push, sign

from verifair import db

REVIEWER = {"vf_id": "review00", "vf_name": "Reviewer"}
SIGNER = {"vf_id": "signer00", "vf_name": "Signer"}


def as_(client, who):
    client.cookies.clear()
    for key, value in who.items():
        client.cookies.set(key, value)


def verdict(client, kind="approve", summary="Fine."):
    form = {"version": 1, "verdict": kind, "summary": summary}
    return client.post("/r/prorate/verdict", data=form)


def withdraw(client):
    return client.post("/r/prorate/verdict/withdraw", data={"version": 1})


def test_after_approving_the_button_becomes_unapprove(client):
    push(client)
    sign(client)
    as_(client, REVIEWER)
    page = verdict(client).text
    form = page.split('id="vf_verdict"')[1].split("</form>")[0]
    assert ">Unapprove</button>" in form and ">Approve</button>" not in form
    assert 'formaction="/r/prorate/verdict/withdraw"' in form
    assert ">Request changes</button>" in form


def test_unapprove_puts_the_review_back_to_waiting(client):
    push(client)
    sign(client)
    as_(client, REVIEWER)
    verdict(client)
    assert api_report(client)["status"] == "approved"
    page = withdraw(client).text
    report = api_report(client)
    assert (report["status"], report["verdicts"]) == ("awaiting_review", [])
    form = page.split('id="vf_verdict"')[1].split("</form>")[0]
    assert ">Approve</button>" in form and "Unapprove" not in form
    assert verdict(client).status_code == 200 and api_report(client)["status"] == "approved"


def test_withdraw_request_changes_and_both_earlier_verdicts_go(client):
    push(client)
    sign(client)
    as_(client, REVIEWER)
    verdict(client)
    page = verdict(client, "request_changes", "No.").text
    assert ">Withdraw request</button>" in page.split('id="vf_verdict"')[1].split("</form>")[0]
    withdraw(client)
    assert api_report(client)["status"] == "awaiting_review"


def test_withdrawing_without_a_verdict_changes_nothing(client):
    push(client)
    sign(client)
    as_(client, REVIEWER)
    assert withdraw(client).status_code == 200
    assert api_report(client)["status"] == "awaiting_review"


def test_withdrawn_verdicts_stay_in_the_database(client, settings):
    push(client)
    sign(client)
    as_(client, REVIEWER)
    verdict(client)
    withdraw(client)
    with db.connect(settings.database_url) as conn:
        [row] = conn.execute("SELECT verdict, withdrawn_at FROM verdicts").fetchall()
    assert row["verdict"] == "approve" and row["withdrawn_at"] is not None


def test_signer_can_unsign_until_someone_gives_a_verdict(client):
    push(client)
    sign(client)
    as_(client, SIGNER)
    page = client.get("/r/prorate").text
    assert 'action="/r/prorate/unsign"' in page
    as_(client, REVIEWER)
    assert 'action="/r/prorate/unsign"' not in client.get("/r/prorate").text
    assert client.post("/r/prorate/unsign", data={"version": 1}).status_code == 403
    as_(client, SIGNER)
    client.post("/r/prorate/unsign", data={"version": 1})
    assert api_report(client)["status"] == "unsigned"
    sign(client)
    as_(client, REVIEWER)
    verdict(client)
    as_(client, SIGNER)
    assert 'action="/r/prorate/unsign"' not in client.get("/r/prorate").text
    r = client.post("/r/prorate/unsign", data={"version": 1})
    assert r.status_code == 409 and api_report(client)["status"] == "approved"
    as_(client, REVIEWER)
    withdraw(client)
    as_(client, SIGNER)
    client.post("/r/prorate/unsign", data={"version": 1})
    assert api_report(client)["status"] == "unsigned"


def test_admin_can_unsign_someone_elses_signature(shared_client, as_user, shared_settings):
    owner = as_user("owner@example.com")
    with db.connect(shared_settings.database_url) as conn:
        db.push_report(
            conn,
            project="p",
            title="T",
            risk="low",
            body="## Intent\nx",
            id="r",
            owner_id="owner@example.com",
        )
        db.sign_version(
            conn, report_id="r", version=1, signer_id="owner@example.com", signer_name="owner"
        )
    other = as_user("other@example.com")
    assert shared_client.post("/r/r/unsign", data={"version": 1}, headers=other).status_code == 403
    assert 'action="/r/r/unsign"' in shared_client.get("/r/r", headers=owner).text
    admin = signed_in("admin@example.com")
    assert shared_client.post("/r/r/unsign", data={"version": 1}, headers=admin).status_code == 200
    with db.connect(shared_settings.database_url) as conn:
        assert db.get_signature(conn, "r", 1) is None


def _plan_with_implementation(settings):
    with db.connect(settings.database_url) as conn:
        db.push_report(
            conn,
            project="p",
            title="P",
            risk="low",
            body=None,
            body_supplied=False,
            workflow="plan_first",
            plan="# Plan",
            id="pf",
        )
        db.sign_version(
            conn,
            report_id="pf",
            version=1,
            signer_id="signer00",
            signer_name="Signer",
            target="plan",
        )
        db.add_verdict(
            conn,
            report_id="pf",
            version=1,
            target="plan",
            reviewer_id="review00",
            reviewer_name="Reviewer",
            verdict="approve",
        )
        return conn


def test_plan_approval_can_be_withdrawn_before_the_implementation_only(client, settings):
    push(client)
    _plan_with_implementation(settings)
    as_(client, REVIEWER)
    plan = {"version": 1, "target": "plan", "revision": 1}
    page = client.get("/r/pf?tab=plan").text
    assert "Unapprove" in page
    with db.connect(settings.database_url) as conn:
        db.push_report(
            conn,
            project="p",
            title="P",
            risk="low",
            body="## Intent\nx",
            id="pf",
            expected_version=1,
        )
    page = client.get("/r/pf?tab=plan").text
    assert "Unapprove" not in page
    r = client.post("/r/pf/verdict/withdraw", data=plan | {"version": 2})
    assert r.status_code == 409 and "Push a new plan revision" in r.json()["detail"]
    as_(client, SIGNER)
    assert 'action="/r/pf/unsign"' not in client.get("/r/pf?tab=plan").text
    assert client.post("/r/pf/unsign", data=plan | {"version": 2}).status_code == 409


def test_withdrawing_frees_the_reviewer_and_posts_to_slack(client, monkeypatch):
    from verifair import notify

    sent = []
    monkeypatch.setattr(notify, "send", lambda settings, text: sent.append(text))
    push(client)
    sign(client)
    as_(client, REVIEWER)
    verdict(client)
    withdraw(client)
    assert sent[-1].startswith("Reviewer withdrew their verdict on Prorate")
    reviewers = client.get("/r/prorate").text.split("<h2>Reviewers</h2>")[1].split("<h2>")[0]
    assert "approved" not in reviewers
