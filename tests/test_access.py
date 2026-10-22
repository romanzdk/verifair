import re
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from verifair import db, web

ADMIN = {"X-Forwarded-Email": "admin@example.com"}
LAST_ADMIN = "There must be at least one active admin."
EXISTS = "That member already exists."
COPY_ONCE = "Copy it now. It will not be shown again."
LOCAL_NOTE = "Access management needs a sign-in proxy. See the README."
ALREADY_CREATED = (
    "This token was already created. Its secret was shown only once."
    " If you did not copy it, revoke it and create a new one."
)


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
        " role=admin active=1 tokens_revoked=0",
        "access action=update_user actor=admin@example.com target=carol@example.com"
        " role=member active=1 tokens_revoked=0",
        "access action=update_user actor=admin@example.com target=carol@example.com"
        " role=member active=0 tokens_revoked=1",
        "access action=update_user actor=admin@example.com target=carol@example.com"
        " role=member active=1 tokens_revoked=0",
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


def _nonce(html):
    return re.search(r'name="nonce" value="([^"]+)"', html).group(1)


def test_resending_the_new_token_form_does_not_create_a_second_token(shared_client, conn):
    page = shared_client.get("/access", headers=ADMIN).text
    nonce = _nonce(page)
    assert _nonce(shared_client.get("/access", headers=ADMIN).text) != nonce
    data = {"label": "laptop", "nonce": nonce}
    first = shared_client.post("/access/tokens", data=data, headers=ADMIN)
    assert 'value="vf_' in first.text and ALREADY_CREATED not in first.text
    assert _nonce(first.text) != nonce
    again = shared_client.post("/access/tokens", data=data, headers=ADMIN)
    assert again.status_code == 200 and again.headers["cache-control"] == "no-store"
    assert ALREADY_CREATED in again.text and "vf_" not in again.text
    assert COPY_ONCE not in again.text
    assert len(db.list_tokens(conn)) == 1


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


def test_member_deactivated_mid_request_gets_the_denied_page(
    shared_client, conn, as_user, monkeypatch
):
    bob = as_user("bob@example.com")
    create = db.create_token

    def deactivate_then_create(conn, **kwargs):
        db.update_user(conn, "bob@example.com", active=False)
        return create(conn, **kwargs)

    monkeypatch.setattr(db, "create_token", deactivate_then_create)
    r = shared_client.post("/access/tokens", data={"label": "laptop"}, headers=bob)
    assert r.status_code == 403 and r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    assert "Ask an admin to reactivate bob@example.com." in r.text
    assert '<a href="/">' not in r.text
    assert db.list_tokens(conn) == []


def test_browser_check_refusals_on_access_keep_the_security_headers(
    shared_client, shared_settings, conn
):
    from fastapi.testclient import TestClient

    from verifair.app import create_app

    bare = TestClient(create_app(shared_settings), headers=ADMIN)
    for r in [
        shared_client.post(
            "/access/tokens", data={"label": "x"}, headers=ADMIN | {"Sec-Fetch-Site": "cross-site"}
        ),
        bare.post("/access/tokens", data={"label": "x"}),
    ]:
        assert r.status_code == 403
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["x-content-type-options"] == "nosniff"
    assert db.list_tokens(conn) == []


@pytest.mark.parametrize(
    "id",
    [
        "../tokens/x/revoke",
        "a/b",
        "a\\b",
        "a\nb",
        "a\x00b",
        "a\x7fb",
        "a b",
        "a\tb",
        ".",
        "..",
    ],
)
def test_add_member_refuses_ids_that_are_not_plain(shared_client, conn, id):
    r = shared_client.post("/access/users", data={"id": id, "name": "X"}, headers=ADMIN)
    assert r.status_code == 422
    assert "Use a plain id such as jane@example.com." in r.text
    assert [u["id"] for u in db.list_users(conn)] == ["admin@example.com"]


@pytest.mark.parametrize("id", ["agent", "Agent", " AGENT "])
def test_add_member_says_the_agent_id_is_reserved(shared_client, conn, id):
    r = shared_client.post("/access/users", data={"id": id, "name": "X"}, headers=ADMIN)
    assert r.status_code == 422
    error = (
        '<p class="error" id="vf_member_error" role="alert">The id agent is reserved for agents.'
    )
    assert f"{error}</p>" in r.text
    assert "Use a plain id" not in r.text.split("<h2>Add member</h2>")[1].split("<form")[0]
    assert [u["id"] for u in db.list_users(conn)] == ["admin@example.com"]


def test_member_id_with_plus_and_at_round_trips_through_row_actions(shared_client, conn):
    id = "jane+ci@example.com"
    r = shared_client.post("/access/users", data={"id": id, "name": "Jane"}, headers=ADMIN)
    assert r.status_code == 200 and id in r.text
    action = "/access/users/jane%2Bci%40example.com"
    assert r.text.count(f'action="{action}"') == 2
    for data, expected in (({"role": "admin"}, ("admin", 1)), ({"active": "0"}, ("admin", 0))):
        r = shared_client.post(action, data=data, headers=ADMIN, follow_redirects=False)
        assert r.status_code == 303
        row = db.get_user(conn, id)
        assert (row["role"], row["active"]) == expected


def test_existing_id_with_a_slash_is_encoded_in_row_actions(shared_client, conn):
    db.add_user(conn, id="team/ops", name="Ops", role="member")
    page = shared_client.get("/access", headers=ADMIN).text
    assert page.count('action="/access/users/team%2Fops"') == 2
    r = shared_client.post(
        "/access/users/team%2Fops", data={"role": "admin"}, headers=ADMIN, follow_redirects=False
    )
    assert r.status_code == 303 and db.get_user(conn, "team/ops")["role"] == "admin"


def test_member_ids_are_stored_lower_case_and_compared_case_insensitively(shared_client, conn):
    r = shared_client.post(
        "/access/users",
        data={"id": " Bob@X.com ", "name": "Bob"},
        headers=ADMIN,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert [u["id"] for u in db.list_users(conn)] == ["admin@example.com", "bob@x.com"]
    r = shared_client.post(
        "/access/users", data={"id": "bob@x.com", "name": "Again"}, headers=ADMIN
    )
    assert r.status_code == 409 and EXISTS in r.text
    r = shared_client.post("/access/users", data={"id": "Agent", "name": "A"}, headers=ADMIN)
    assert r.status_code == 422 and db.get_user(conn, "agent") is None
    with pytest.raises(ValueError):
        db.add_user(conn, id="BOB@x.com", name="Bob", role="member")


def test_access_tables_stack_with_cell_labels_on_narrow_screens(shared_client, conn):
    _token(conn, "admin@example.com")
    page = shared_client.get("/access", headers=ADMIN).text
    for label in ("Owner", "Label", "Token id", "Created", "Status", "Id", "Name", "Role"):
        assert f'<td data-label="{label}">' in page, label
    assert '<td data-label="Active">Yes</td>' in page
    css = (Path(web.__file__).parent / "static" / "landing.css").read_text()
    assert ".landing-access td { vertical-align: middle; overflow-wrap: break-word; }" in css
    assert "anywhere" not in css.split(".landing-access td {")[1].split("}")[0]
    mobile = css.split("@media (max-width: 760px) {")[1].split("\n}")[0]
    assert ".landing-access :is(table, tbody, tr, td) { display: block; }" in mobile
    assert "content: attr(data-label)" in mobile
    link = css.split(".landing-access-link {")[1].split("}")[0]
    assert "display: inline-flex" in link and "min-height: 32px" in link
    assert "align-items: center" in link and "padding:" in link


def test_row_buttons_are_named_and_destructive_ones_ask_first(shared_client, conn, as_user):
    as_user("bob@example.com")
    db.add_user(conn, id="gone@example.com", name="Gone", role="member")
    db.update_user(conn, "gone@example.com", active=False)
    _token(conn, "bob@example.com", 'bob "ci"')
    page = shared_client.get("/access", headers=ADMIN).text
    assert '<button class="btn" aria-label="Save role for bob@example.com">Save</button>' in page
    assert (
        '<button class="btn" aria-label="Deactivate bob@example.com"'
        ' data-confirm="Deactivate bob@example.com? Their agent tokens will be revoked.">'
        "Deactivate</button>"
    ) in page
    assert '<button class="btn" aria-label="Reactivate gone@example.com">Reactivate</button>' in (
        page
    )
    assert (
        '<button class="btn" aria-label="Revoke token bob &#34;ci&#34;"'
        ' data-confirm="Revoke token bob &#34;ci&#34;?">Revoke</button>'
    ) in page
    assert page.count("(you)") == 1
    assert "<code>admin@example.com</code> <small>(you)</small>" in page
    assert (
        '<button class="btn" aria-label="Deactivate admin@example.com"'
        ' data-confirm="Deactivate yourself? You will lose access right away.">Deactivate</button>'
    ) in page
    js = (Path(web.__file__).parent / "static" / "landing.js").read_text()
    assert 'addEventListener("submit"' in js and "event.submitter?.dataset.confirm" in js
    assert "!confirm(message)" in js and "event.preventDefault()" in js


PLAIN = "Use a plain id such as jane@example.com."
MEMBER_INPUT = "Enter an id (1–200 characters) and a name (1–60 characters)."


@pytest.mark.parametrize(
    ("data", "status", "copy"),
    [
        ({"id": "a b", "name": "Eve Q", "role": "admin"}, 422, PLAIN),
        ({"id": "Admin@example.com", "name": "Again", "role": "admin"}, 409, EXISTS),
        ({"id": "eve@example.com", "name": " ", "role": "admin"}, 422, MEMBER_INPUT),
    ],
)
def test_add_member_errors_show_in_the_card_and_keep_the_input(shared_client, data, status, copy):
    r = shared_client.post("/access/users", data=data, headers=ADMIN)
    assert r.status_code == status
    before, card = r.text.split("<h2>Add member</h2>")
    card = card.split("</section>")[0]
    error = f'<p class="error" id="vf_member_error" role="alert">{copy}</p>'
    assert error in card and copy not in before
    assert before.endswith('<section class="landing-access" id="add-member">\n  ')
    assert '<form method="post" action="/access/users#add-member"' in card
    assert (
        f'name="id" value="{data["id"]}" autofocus'
        ' aria-invalid="true" aria-describedby="vf_member_error"'
    ) in card
    assert f'name="name" value="{data["name"]}"' in card
    assert '<option value="admin" selected>' in card
    assert '<option value="member" selected>' not in card
    js = (Path(web.__file__).parent / "static" / "landing.js").read_text()
    assert 'addEventListener("load", () => document.querySelector("[autofocus]")?.focus());' in js


def test_add_member_inputs_check_the_id_in_the_browser(shared_client):
    page = shared_client.get("/access", headers=ADMIN).text
    card = page.split("<h2>Add member</h2>")[1].split("</section>")[0]
    assert f'name="id" value="" maxlength="200" pattern="[^\\s\\/\\\\]+" title="{PLAIN}"' in card
    assert 'name="name" value="" maxlength="60"' in card
    assert '<option value="member" selected>' in card
    assert 'class="error"' not in card and "autofocus" not in page
    assert "aria-invalid" not in card and "vf_member_error" not in card


def test_denied_page_has_a_heading_and_links_back(shared_client, conn, as_user):
    bob = as_user("bob@example.com")
    as_user("gone@example.com")
    db.update_user(conn, "gone@example.com", active=False)
    for r, back, home in [
        (
            shared_client.post("/access/users", data={"id": "e", "name": "E"}, headers=bob),
            True,
            True,
        ),
        (shared_client.get("/", headers={"X-Forwarded-Email": "x@example.com"}), False, False),
        (shared_client.get("/", headers={"X-Forwarded-Email": "gone@example.com"}), False, False),
    ]:
        assert r.status_code == 403
        assert '<main class="landing-wrap">' in r.text and "<h1>No access</h1>" in r.text
        assert 'href="/static/landing.css"' in r.text
        assert ('<a href="/">' in r.text) is home
        assert ('<a href="/access">' in r.text) is back
        assert "<script" not in r.text and 'class="topbar"' not in r.text
    signin = shared_client.get("/", headers={"X-Forwarded-Email": ""})
    assert signin.status_code == 401 and '<a href="/">Go to projects</a>' in signin.text


def test_new_token_has_a_copy_button_and_the_note_describes_the_field(shared_client):
    r, token = _new_token(shared_client, ADMIN)
    assert (
        f'<input readonly value="{token}" id="vf_new_token" aria-label="New token"'
        ' aria-describedby="vf_new_token_note"'
    ) in r.text
    assert f'<p id="vf_new_token_note">{COPY_ONCE}</p>' in r.text
    assert (
        '<button type="button" class="btn" data-copy-target="vf_new_token">Copy token</button>'
        ' <span data-copy-result role="status"></span>'
    ) in r.text
    assert r.text.count(token) == 1
    js = (Path(web.__file__).parent / "static" / "landing.js").read_text()
    assert '"[data-copy-identity], [data-copy-target]"' in js and "Token copied." in js


def test_tokens_revoked_counts_what_the_deactivation_revoked(
    shared_client, conn, as_user, monkeypatch, caplog
):
    caplog.set_level("INFO", logger="verifair")
    as_user("bob@example.com")
    update = db.update_user

    def create_then_update(conn, id, **kwargs):
        _token(conn, id, "late")
        return update(conn, id, **kwargs)

    monkeypatch.setattr(db, "update_user", create_then_update)
    r = shared_client.post(
        "/access/users/bob@example.com", data={"active": "0"}, headers=ADMIN, follow_redirects=False
    )
    assert r.status_code == 303
    assert (
        "access action=update_user actor=admin@example.com target=bob@example.com"
        " role=member active=0 tokens_revoked=1"
    ) in caplog.text
