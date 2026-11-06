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
