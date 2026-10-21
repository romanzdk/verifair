import re
from contextlib import closing
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from verifair import db, web

ADMIN = {"X-Forwarded-Email": "admin@example.com"}
LAST_ADMIN = "There must be at least one active admin."
EXISTS = "That member already exists."
COPY_ONCE = "Copy it now. It will not be shown again."
LOCAL_NOTE = "Access management needs a sign-in proxy. See the README."


@pytest.fixture
def conn(shared_client, shared_settings):
    with closing(db.connect(shared_settings.db_path)) as conn:
        yield conn


def _token(conn, owner_id, label="agent"):
    return db.create_token(conn, owner_id=owner_id, label=label)


def _new_token(client, headers, label="laptop"):
    r = client.post("/access/tokens", data={"label": label}, headers=headers)
    assert r.status_code == 200
    return r, re.search(r'value="(vf_[^"]+)"', r.text).group(1)


def test_member_sees_only_own_tokens_and_no_admin_sections(shared_client, conn, as_user):
    bob = as_user("bob@example.com")
    _, mine = _token(conn, "bob@example.com", "bob laptop")
    _, theirs = _token(conn, "admin@example.com", "admin ci")
    page = shared_client.get("/access", headers=bob)
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "Your agent tokens" in page.text and mine["id"] in page.text
    assert "bob laptop" in page.text and 'action="/access/tokens"' in page.text
    assert theirs["id"] not in page.text and "admin ci" not in page.text
    assert "Members" not in page.text and "All tokens" not in page.text
    assert 'action="/access/users' not in page.text

    admin = shared_client.get("/access", headers=ADMIN).text
    assert "Members" in admin and "Add member" in admin and "All tokens" in admin
    assert mine["id"] in admin and theirs["id"] in admin
    assert 'action="/access/users/bob%40example.com"' in admin


def test_member_cannot_manage_members_or_revoke_other_tokens(shared_client, conn, as_user):
    bob = as_user("bob@example.com")
    _, theirs = _token(conn, "admin@example.com")
    for path, data in [
        ("/access/users", {"id": "eve@example.com", "name": "Eve", "role": "admin"}),
        ("/access/users/bob@example.com", {"role": "admin"}),
        ("/access/users/admin@example.com", {"active": "0"}),
        (f"/access/tokens/{theirs['id']}/revoke", None),
    ]:
        r = shared_client.post(path, data=data, headers=bob, follow_redirects=False)
        assert r.status_code == 403, path
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["cache-control"] == "no-store"
    assert db.get_user(conn, "eve@example.com") is None
    assert db.get_user(conn, "bob@example.com")["role"] == "member"
    assert db.get_user(conn, "admin@example.com")["active"] == 1
    assert db.get_token(conn, theirs["id"])["revoked_at"] is None
    assert shared_client.post("/access/tokens/nope/revoke", headers=bob).status_code == 404
    r = shared_client.post(
        "/access/users/nobody@example.com", data={"role": "admin"}, headers=ADMIN
    )
    assert r.status_code == 404


def test_admin_adds_promotes_deactivates_and_reactivates(shared_client, conn, caplog):
    caplog.set_level("INFO", logger="verifair")
    r = shared_client.post(
        "/access/users",
        data={"id": "  carol@example.com ", "name": " Carol C ", "role": "member"},
        headers=ADMIN,
        follow_redirects=False,
    )
    assert (r.status_code, r.headers["location"]) == (303, "/access")
    carol = db.get_user(conn, "carol@example.com")
    assert (carol["name"], carol["role"], carol["active"]) == ("Carol C", "member", 1)
    who = {"X-Forwarded-Email": "carol@example.com"}
    assert shared_client.get("/", headers=who).status_code == 200
    token, old = _token(conn, "carol@example.com")

    r = shared_client.post(
        "/access/users", data={"id": "carol@example.com", "name": "Again"}, headers=ADMIN
    )
    assert r.status_code == 409 and EXISTS in r.text
    assert db.get_user(conn, "carol@example.com")["name"] == "Carol C"

    def update(**data):
        r = shared_client.post(
            "/access/users/carol@example.com", data=data, headers=ADMIN, follow_redirects=False
        )
        assert (r.status_code, r.headers["location"]) == (303, "/access")
        return db.get_user(conn, "carol@example.com")

    assert update(role="admin")["role"] == "admin"
    assert update(role="member")["role"] == "member"
    assert update(active="0")["active"] == 0
    assert shared_client.get("/access", headers=who).status_code == 403
    assert db.get_token(conn, old["id"])["revoked_at"] is not None
    assert update(active="1")["active"] == 1
    assert shared_client.get("/access", headers=who).status_code == 200
    assert db.get_token(conn, old["id"])["revoked_at"] is not None
    api = shared_client.get("/api/reports/x", headers={"Authorization": f"Bearer {token}"})
    assert api.json() == {"detail": "invalid token"}

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("access ")]
    assert lines == [
        "access action=add_user actor=admin@example.com target=carol@example.com role=member",
        "access action=update_user actor=admin@example.com target=carol@example.com"
        " role=admin active=1",
        "access action=update_user actor=admin@example.com target=carol@example.com"
        " role=member active=1",
        "access action=update_user actor=admin@example.com target=carol@example.com"
        " role=member active=0",
        "access action=update_user actor=admin@example.com target=carol@example.com"
        " role=member active=1",
    ]


@pytest.mark.parametrize(
    "data",
    [
        {"id": " ", "name": "Eve"},
        {"id": "e" * 201, "name": "Eve"},
        {"id": "eve@example.com", "name": "  "},
        {"id": "eve@example.com", "name": "E" * 61},
    ],
)
def test_add_member_validates_id_and_name(shared_client, conn, data):
    r = shared_client.post("/access/users", data=data | {"role": "member"}, headers=ADMIN)
    assert r.status_code == 422
    assert "Enter an id (1–200 characters) and a name (1–60 characters)." in r.text
    assert [u["id"] for u in db.list_users(conn)] == ["admin@example.com"]


def test_add_member_accepts_the_limits(shared_client, conn):
    data = {"id": "e" * 200, "name": "E" * 60, "role": "admin"}
    r = shared_client.post("/access/users", data=data, headers=ADMIN, follow_redirects=False)
    assert r.status_code == 303
    assert db.get_user(conn, "e" * 200)["role"] == "admin"


def test_last_admin_cannot_demote_or_deactivate_themselves(shared_client, conn):
    for data in ({"role": "member"}, {"active": "0"}):
        r = shared_client.post("/access/users/admin@example.com", data=data, headers=ADMIN)
        assert r.status_code == 409 and LAST_ADMIN in r.text
        assert r.headers["cache-control"] == "no-store"
        admin = db.get_user(conn, "admin@example.com")
        assert (admin["role"], admin["active"]) == ("admin", 1)


def test_admin_may_deactivate_themselves_when_another_admin_exists(shared_client, conn):
    db.add_user(conn, id="second@example.com", name="Second", role="admin")
    r = shared_client.post(
        "/access/users/admin@example.com",
        data={"active": "0"},
        headers=ADMIN,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert shared_client.get("/access", headers=ADMIN).status_code == 403


def test_new_token_is_shown_once_and_never_logged(shared_client, conn, as_user, caplog):
    caplog.set_level("DEBUG")
    bob = as_user("bob@example.com")
    r, token = _new_token(shared_client, bob, " laptop ")
    assert r.headers["cache-control"] == "no-store" and "location" not in r.headers
    assert f'<input readonly value="{token}"' in r.text and COPY_ONCE in r.text
    [row] = db.list_tokens(conn, "bob@example.com")
    assert (row["label"], token.startswith(f"vf_{row['id']}.")) == ("laptop", True)
    again = shared_client.get("/access", headers=bob)
    assert again.headers["cache-control"] == "no-store"
    assert token not in again.text and COPY_ONCE not in again.text and row["id"] in again.text
    api = shared_client.get("/api/reports/x", headers={"Authorization": f"Bearer {token}"})
    assert api.status_code == 404
    secret = token.partition(".")[2]
    assert secret not in caplog.text
    assert f"access action=create_token actor=bob@example.com target={row['id']}" in caplog.text


@pytest.mark.parametrize("label", ["", "   ", "x" * 61])
def test_new_token_needs_a_label(shared_client, conn, label):
    r = shared_client.post("/access/tokens", data={"label": label}, headers=ADMIN)
    assert r.status_code == 422 and "Enter a label (1–60 characters)." in r.text
    assert db.list_tokens(conn) == []


def test_owner_and_admin_revoke_tokens(shared_client, conn, as_user, caplog):
    caplog.set_level("INFO", logger="verifair")
    bob = as_user("bob@example.com")
    _, first = _token(conn, "bob@example.com")
    _, second = _token(conn, "bob@example.com")
    for token, who in ((first, bob), (second, ADMIN)):
        r = shared_client.post(
            f"/access/tokens/{token['id']}/revoke", headers=who, follow_redirects=False
        )
        assert (r.status_code, r.headers["location"]) == (303, "/access")
        assert db.get_token(conn, token["id"])["revoked_at"] is not None
    page = shared_client.get("/access", headers=bob).text
    assert page.count("Revoked") == 2 and f"/access/tokens/{first['id']}/revoke" not in page
    assert f"access action=revoke_token actor=bob@example.com target={first['id']}" in caplog.text
    assert f"access action=revoke_token actor=admin@example.com target={second['id']}" in (
        caplog.text
    )


def test_local_mode_access_is_a_note_and_posts_are_404(client):
    page = client.get("/access")
    assert page.status_code == 200 and LOCAL_NOTE in page.text
    assert page.headers["cache-control"] == "no-store"
    assert 'action="/access' not in page.text
    for path, data in [
        ("/access/users", {"id": "eve", "name": "Eve", "role": "member"}),
        ("/access/users/eve", {"role": "admin"}),
        ("/access/tokens", {"label": "laptop"}),
        ("/access/tokens/abc/revoke", None),
    ]:
        r = client.post(path, data=data)
        assert r.status_code == 404, path
        assert r.headers["cache-control"] == "no-store"


def test_access_link_on_landing_pages_in_shared_mode_only(shared_client, conn, client):
    db.push_report(conn, project="p", title="T", risk="low", body="## A\nx", id="r")
    for path in ("/", "/p/p"):
        assert 'href="/access"' in shared_client.get(path, headers=ADMIN).text, path
    assert "/access" not in shared_client.get("/r/r", headers=ADMIN).text
    assert 'href="/access"' not in client.get("/").text


def test_report_context_refuses_moderation_without_a_member_in_shared_mode(conn):
    db.push_report(conn, project="p", title="T", risk="low", body="## A\nx", id="r")
    db.add_comment(
        conn, report_id="r", version=1, author_id="x", author_name="x", body="b", section="A"
    )
    report = db.get_report(conn, "r")
    assert web.report_context(conn, report, 1, "x", None, shared=True)["can_moderate"] == set()
    assert web.report_context(conn, report, 1, "x", None, shared=False)["can_moderate"] != set()


def test_current_user_refuses_a_missing_member_in_shared_mode(shared_settings):
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=shared_settings)),
        state=SimpleNamespace(member=None),
        cookies={"vf_id": "mallory0", "vf_name": "Mallory"},
    )
    with pytest.raises(HTTPException) as denied:
        web.current_user(request)
    assert denied.value.status_code == 401
