from contextlib import closing

import pytest
from conftest import signed_in

from verifair import db

ADMIN = signed_in("admin@example.com")
NO_REDIRECT = {"follow_redirects": False}


@pytest.fixture
def conn(shared_client, shared_settings):
    with closing(db.connect(shared_settings.database_url)) as conn:
        yield conn


@pytest.fixture
def team(conn, as_user):
    people = {who: as_user(f"{who}@example.com") for who in ("owner", "rev", "other")}
    for id in ("r", "r2"):
        db.push_report(
            conn, project="p", title=id, risk="low", body="x", id=id, owner_id="owner@example.com"
        )
    db.add_review_request(conn, "r", "rev@example.com")
    return people


def token(conn, owner):
    return {"Authorization": f"Bearer {db.create_token(conn, owner_id=owner, label='a')[0]}"}


@pytest.mark.parametrize(("who", "ok"), [("owner", True), ("rev", True), ("other", False)])
def test_owner_requested_reviewers_and_admins_archive(shared_client, conn, team, who, ok):
    page = shared_client.get("/r/r", headers=team[who]).text
    assert ('action="/r/r/archive"' in page) is ok
    r = shared_client.post("/r/r/archive", headers=team[who], **NO_REDIRECT)
    assert r.status_code == (303 if ok else 403)
    assert (db.get_report(conn, "r")["archived_at"] is not None) is ok
    if ok:
        assert db.get_report(conn, "r")["archived_by"] == who
        assert (
            shared_client.post("/r/r/restore", headers=team[who], **NO_REDIRECT).status_code == 303
        )
        assert db.get_report(conn, "r")["archived_at"] is None
    assert shared_client.post("/r/r/archive", headers=ADMIN, **NO_REDIRECT).status_code == 303


def test_archived_reviews_leave_the_lists_but_stay_readable(shared_client, conn, team):
    shared_client.post("/r/r/archive", headers=team["owner"])
    home = shared_client.get("/", headers=team["rev"]).text
    assert 'href="/r/r"' not in home and 'href="/r/r2"' not in home
    listed = shared_client.get("/changes?tab=all", headers=team["owner"]).text
    assert 'href="/r/r"' not in listed and 'href="/r/r2"' in listed
    archived = shared_client.get("/p/p?tab=archived", headers=team["owner"]).text
    assert 'href="/r/r"' in archived and 'href="/r/r2"' not in archived
    assert 'aria-label="Archived · 1"' in archived
    page = shared_client.get("/r/r", headers=team["other"])
    assert page.status_code == 200 and "Archived by owner" in page.text
    assert 'action="/r/r/restore"' not in page.text
    assert 'action="/r/r/restore"' in shared_client.get("/r/r", headers=team["rev"]).text


def test_pushing_to_an_archived_review_is_refused(shared_client, conn, team):
    db.set_archived(conn, "r", "owner")
    payload = {"id": "r", "project": "p", "title": "r", "risk": "low", "body": "y"}
    r = shared_client.post("/api/reports", json=payload, headers=token(conn, "owner@example.com"))
    assert r.status_code == 409
    assert r.json()["detail"] == "This report is archived. Restore it before pushing a new version."


def test_agents_archive_and_restore_through_the_api(shared_client, conn, team):
    owner, rev = token(conn, "owner@example.com"), token(conn, "rev@example.com")
    assert (
        shared_client.post(
            "/api/reports/r/archive", headers=token(conn, "other@example.com")
        ).status_code
        == 403
    )
    r = shared_client.post("/api/reports/r/archive", headers=rev)
    assert r.status_code == 200 and r.json()["archived_by"] == "rev"
    assert shared_client.get("/api/reports/r", headers=owner).json()["archived_at"] is not None
    r = shared_client.post("/api/reports/r/restore", headers=owner)
    assert (r.json()["archived_at"], r.json()["archived_by"]) == (None, None)


def test_only_admins_delete_and_they_must_type_the_id(shared_client, conn, team):
    db.add_comment(conn, report_id="r", version=1, author_id="rev", author_name="rev", body="hm")
    db.sign_version(
        conn, report_id="r", version=1, signer_id="owner@example.com", signer_name="owner"
    )
    assert 'href="/r/r/delete"' not in shared_client.get("/r/r", headers=team["owner"]).text
    assert shared_client.get("/r/r/delete", headers=team["owner"]).status_code == 403
    form = {"confirm": "r"}
    assert shared_client.post("/r/r/delete", data=form, headers=team["owner"]).status_code == 403
    assert 'href="/r/r/delete"' in shared_client.get("/r/r", headers=ADMIN).text
    wrong = shared_client.post("/r/r/delete", data={"confirm": "r2"}, headers=ADMIN)
    assert wrong.status_code == 422 and "Type the review id exactly" in wrong.text
    r = shared_client.post("/r/r/delete", data=form, headers=ADMIN, **NO_REDIRECT)
    assert (r.status_code, r.headers["location"]) == (303, "/")
    assert db.get_report(conn, "r") is None and db.get_report(conn, "r2") is not None
    for table in ("versions", "comments", "signatures", "review_requests"):
        assert conn.execute(f"SELECT 1 FROM {table} WHERE report_id='r'").fetchone() is None
    assert shared_client.get("/r/r", headers=ADMIN).status_code == 404


def test_only_admins_archive_a_whole_project(shared_client, conn, team):
    assert "Archive project" not in shared_client.get("/p/p", headers=team["owner"]).text
    assert shared_client.post("/p/p/archive", headers=team["owner"]).status_code == 403
    assert "Archive project" in shared_client.get("/p/p", headers=ADMIN).text
    r = shared_client.post("/p/p/archive", headers=ADMIN, **NO_REDIRECT)
    assert (r.status_code, r.headers["location"]) == (303, "/p/p?tab=archived")
    assert all(db.get_report(conn, id)["archived_at"] for id in ("r", "r2"))


def test_local_mode_lets_anyone_archive_and_delete(client):
    payload = {"id": "loc", "project": "p", "title": "Loc", "risk": "low", "body": "x"}
    client.post("/api/reports", json=payload, headers={"Authorization": "Bearer t"})
    page = client.get("/r/loc").text
    assert 'action="/r/loc/archive"' in page and 'href="/r/loc/delete"' in page
    assert client.post("/r/loc/archive", **NO_REDIRECT).status_code == 303
    r = client.post("/api/reports/loc/restore", headers={"Authorization": "Bearer t"})
    assert r.json()["archived_at"] is None
    assert client.post("/r/loc/delete", data={"confirm": "loc"}, **NO_REDIRECT).status_code == 303
    assert client.get("/r/loc").status_code == 404
