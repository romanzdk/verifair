import json
import os
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import replace
from urllib.parse import urlsplit

import psycopg
import pytest
from conftest import OIDC, SessionClient, signed_in
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from verifair import app as app_module
from verifair import db
from verifair.app import create_app
from verifair.config import Settings, load_settings


def test_health(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("VERIFAIR_API_TOKEN", raising=False)
    monkeypatch.delenv("VERIFAIR_OIDC_ISSUER", raising=False)
    with pytest.raises(RuntimeError) as error:
        load_settings()
    assert str(error.value) == (
        "VERIFAIR_API_TOKEN is required in local mode. Set VERIFAIR_OIDC_ISSUER for shared mode."
    )


def test_database_url_is_required(monkeypatch):
    monkeypatch.setenv("VERIFAIR_API_TOKEN", "t")
    monkeypatch.delenv("VERIFAIR_DATABASE_URL")
    with pytest.raises(RuntimeError) as error:
        load_settings()
    assert str(error.value) == (
        "VERIFAIR_DATABASE_URL is required,"
        " for example postgresql://verifair:verifair@localhost:5432/verifair"
    )


def test_pool_size_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("VERIFAIR_API_TOKEN", "t")
    monkeypatch.setenv("VERIFAIR_DB_POOL_SIZE", "3")
    assert load_settings().db_pool_size == 3


@pytest.mark.parametrize("value", ["", "ten", "0", "-2", "2.5"])
def test_bad_pool_size_is_a_clear_error(monkeypatch, value):
    monkeypatch.setenv("VERIFAIR_API_TOKEN", "t")
    monkeypatch.setenv("VERIFAIR_DB_POOL_SIZE", value)
    with pytest.raises(RuntimeError) as error:
        load_settings()
    assert str(error.value) == "VERIFAIR_DB_POOL_SIZE must be a whole number of 1 or more."


SMTP_ERROR = (
    "VERIFAIR_SMTP_URL must look like smtp://user:password@host:587"
    " or smtps://user:password@host:465"
)

FROM_ERROR = "VERIFAIR_EMAIL_FROM must be one plain address such as verifair@example.com."


@pytest.mark.parametrize(
    ("url", "sender", "message"),
    [
        ("smtp://mail:1025", "", "VERIFAIR_EMAIL_FROM is required when VERIFAIR_SMTP_URL is set."),
        ("https://u:secret@mail", "v@x.com", SMTP_ERROR),
        ("smtp://", "v@x.com", SMTP_ERROR),
        ("smtp://u:secret@mail:port", "v@x.com", SMTP_ERROR),
        ("smtp://mail:1025", "Verifair <v@x.com>", FROM_ERROR),
        ("smtp://mail:1025", "v@x.com, w@x.com", FROM_ERROR),
        ("smtp://mail:1025", "verifair", FROM_ERROR),
    ],
)
def test_bad_email_settings_are_a_clear_error(monkeypatch, url, sender, message):
    monkeypatch.setenv("VERIFAIR_API_TOKEN", "t")
    monkeypatch.setenv("VERIFAIR_SMTP_URL", url)
    monkeypatch.setenv("VERIFAIR_EMAIL_FROM", sender)
    with pytest.raises(RuntimeError) as error:
        load_settings()
    assert str(error.value) == message


def test_email_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("VERIFAIR_API_TOKEN", "t")
    assert (load_settings().smtp_url, load_settings().email_from) == (None, None)
    monkeypatch.setenv("VERIFAIR_SMTP_URL", "smtps://u:p@mail:465")
    monkeypatch.setenv("VERIFAIR_EMAIL_FROM", " verifair@example.com ")
    s = load_settings()
    assert (s.smtp_url, s.email_from) == ("smtps://u:p@mail:465", "verifair@example.com")


def test_smtp_in_local_mode_logs_that_no_mail_is_sent(settings, make_app, caplog):
    make_app(replace(settings, smtp_url="smtp://mail:1025", email_from="v@x.com"))
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ("WARNING", "VERIFAIR_SMTP_URL is ignored in local mode. Emails need shared mode.")
    ]


def _record_pools(monkeypatch):
    pools = []
    open_pool = db.open_pool

    def recording(*args):
        pools.append(open_pool(*args))
        return pools[-1]

    monkeypatch.setattr(db, "open_pool", recording)
    return pools


def test_local_mode_refuses_a_database_with_members(settings, monkeypatch):
    with closing(db.connect(settings.database_url)) as conn:
        db.init_schema(conn)
        db.add_user(conn, id="admin@example.com", name="admin", role="admin")
    pools = _record_pools(monkeypatch)
    with pytest.raises(RuntimeError) as error:
        create_app(settings)
    assert str(error.value) == (
        "This database has members. Set VERIFAIR_OIDC_ISSUER to run in shared mode."
    )
    assert pools[0].closed


def test_startup_gives_up_cleanly_on_a_lock_timeout(settings, monkeypatch):
    def locked(conn):
        raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")

    monkeypatch.setattr(db, "init_schema", locked)
    pools = _record_pools(monkeypatch)
    with pytest.raises(RuntimeError) as error:
        create_app(settings)
    assert str(error.value) == (
        "Startup waited too long for a database lock held by another transaction."
        " Restart to try again."
    )
    assert pools[0].closed


def test_lifespan_closes_the_pool(settings, make_app):
    app = make_app(settings)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert app.state.pool.closed


def test_startup_logs_the_bootstrap_outcome_and_an_ignored_shared_token(
    shared_settings, make_app, caplog
):
    caplog.set_level("INFO", logger="verifair")
    make_app(shared_settings)
    make_app(replace(shared_settings, api_token="t"))
    make_app(replace(shared_settings, bootstrap_admin_id=None))
    lines = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert lines == [
        ("INFO", "bootstrap admin created id=admin@example.com"),
        ("WARNING", "VERIFAIR_API_TOKEN is ignored in shared mode. Agents need member tokens."),
        ("INFO", "bootstrap skipped: members exist"),
    ]


def _oidc_env(monkeypatch):
    monkeypatch.delenv("VERIFAIR_API_TOKEN", raising=False)
    monkeypatch.setenv("VERIFAIR_OIDC_ISSUER", OIDC["oidc_issuer"])
    monkeypatch.setenv("VERIFAIR_OIDC_CLIENT_ID", OIDC["oidc_client_id"])
    monkeypatch.setenv("VERIFAIR_OIDC_CLIENT_SECRET", OIDC["oidc_client_secret"])
    monkeypatch.setenv("VERIFAIR_SESSION_SECRET", OIDC["session_secret"])


def test_shared_mode_loads_without_api_token(monkeypatch):
    _oidc_env(monkeypatch)
    monkeypatch.setenv("VERIFAIR_BOOTSTRAP_ADMIN_ID", "admin@example.com")
    s = load_settings()
    assert (s.api_token, s.oidc_issuer, s.bootstrap_admin_id) == (
        None,
        OIDC["oidc_issuer"],
        "admin@example.com",
    )


def test_shared_mode_startup_bootstraps_the_first_admin(shared_settings, make_app):
    def users():
        with closing(db.connect(shared_settings.database_url)) as conn:
            db.init_schema(conn)
            return [(u["id"], u["name"], u["role"]) for u in db.list_users(conn)]

    with pytest.raises(RuntimeError) as error:
        create_app(replace(shared_settings, bootstrap_admin_id=None))
    assert str(error.value) == "VERIFAIR_BOOTSTRAP_ADMIN_ID is required to create the first admin"
    assert users() == []
    make_app(shared_settings)
    assert users() == [("admin@example.com", "admin", "admin")]
    make_app(replace(shared_settings, bootstrap_admin_id=None))
    make_app(replace(shared_settings, bootstrap_admin_id="other@example.com"))
    assert users() == [("admin@example.com", "admin", "admin")]


def test_bootstrap_admin_id_is_stored_lower_case(shared_settings, make_app):
    make_app(replace(shared_settings, bootstrap_admin_id="Admin@Example.com"))
    with closing(db.connect(shared_settings.database_url)) as conn:
        assert [u["id"] for u in db.list_users(conn)] == ["admin@example.com"]


@pytest.mark.parametrize("value", ["Agent", "AGENT"])
def test_bootstrap_admin_id_agent_is_refused_in_any_case(shared_settings, value):
    with pytest.raises(RuntimeError, match="must be the email"):
        create_app(replace(shared_settings, bootstrap_admin_id=value))


def test_local_mode_startup_needs_no_users(settings, make_app):
    make_app(settings)
    with closing(db.connect(settings.database_url)) as conn:
        assert db.list_users(conn) == []


def test_bootstrap_admin_id_without_sign_in_refuses_to_start(settings):
    with pytest.raises(RuntimeError) as error:
        create_app(replace(settings, bootstrap_admin_id="admin@example.com"))
    assert str(error.value) == (
        "VERIFAIR_BOOTSTRAP_ADMIN_ID is set but VERIFAIR_OIDC_ISSUER is not."
        " Set up sign-in to run in shared mode."
    )
    with closing(db.connect(settings.database_url)) as conn:
        db.init_schema(conn)
        assert db.list_users(conn) == []


@pytest.mark.parametrize("value", ["", " \n"])
def test_empty_bootstrap_admin_id_is_none(monkeypatch, value):
    monkeypatch.setenv("VERIFAIR_API_TOKEN", "t")
    monkeypatch.setenv("VERIFAIR_BOOTSTRAP_ADMIN_ID", value)
    assert load_settings().bootstrap_admin_id is None


def test_bootstrap_admin_id_is_stripped(monkeypatch, new_database_url, make_app):
    _oidc_env(monkeypatch)
    monkeypatch.setenv("VERIFAIR_DATABASE_URL", new_database_url())
    monkeypatch.setenv("VERIFAIR_BOOTSTRAP_ADMIN_ID", " admin@example.com\n")
    s = load_settings()
    assert s.bootstrap_admin_id == "admin@example.com"
    client = SessionClient(make_app(s), headers=signed_in("admin@example.com"))
    assert client.get("/access").status_code == 200


@pytest.mark.parametrize(
    "value",
    [
        "jane doe@example.com",
        "a/b@x",
        "a\\b@x",
        ".",
        "..",
        "a\nb@x",
        "agent",
        "boss",
        "jän@x.cz",
        "\u212aate@x.cz",
    ],
)
def test_bootstrap_admin_id_must_be_an_email(shared_settings, value):
    with pytest.raises(RuntimeError) as error:
        create_app(replace(shared_settings, bootstrap_admin_id=value))
    assert str(error.value) == (
        "VERIFAIR_BOOTSTRAP_ADMIN_ID must be the email the admin signs in with,"
        " such as jane@example.com"
    )


def test_defaults(monkeypatch):
    monkeypatch.setenv("VERIFAIR_API_TOKEN", "t")
    s = load_settings()
    assert (s.base_url, s.slack_webhook_url, s.db_pool_size) == (
        "http://localhost:8000",
        None,
        10,
    )


def test_base_url_trailing_slash_is_stripped(monkeypatch):
    monkeypatch.setenv("VERIFAIR_API_TOKEN", "t")
    monkeypatch.setenv("VERIFAIR_BASE_URL", "https://review.example.com/")
    assert load_settings().base_url == "https://review.example.com"


H = {"Authorization": "Bearer t"}
REPORT = {"project": "p", "title": "T", "risk": "low", "body": "## A\nx"}


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_cross_origin_web_post_is_refused(client, headers):
    r = client.post("/me", data={"name": "Mallory"}, headers=headers, follow_redirects=False)
    assert r.status_code == 403
    assert "vf_name" not in r.cookies


@pytest.mark.parametrize(
    "headers",
    [
        {"Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "none"},
    ],
)
def test_same_origin_web_post_is_allowed(client, headers):
    r = client.post("/me", data={"name": "Alice"}, headers=headers, follow_redirects=False)
    assert r.status_code == 303


def test_web_post_without_sec_fetch_site_is_refused(settings, make_app):
    curl = TestClient(make_app(settings))
    r = curl.post("/me", data={"name": "Mallory"}, follow_redirects=False)
    assert r.status_code == 403
    assert r.json() == {"detail": "browser request required"}
    assert "vf_name" not in r.cookies


@pytest.mark.parametrize(
    "base_url",
    ["http://0.0.0.0:8000", "http://localhost:8000", "http://127.0.0.2", "http://[::1]:8000"],
)
def test_loopback_web_post_without_sec_fetch_site_is_allowed_from_own_origin(
    settings, make_app, base_url
):
    browser = TestClient(make_app(settings), base_url=base_url)
    r = browser.post(
        "/me", data={"name": "Alice"}, headers={"Origin": base_url}, follow_redirects=False
    )
    assert r.status_code == 303


@pytest.mark.parametrize(
    ("base_url", "origin"),
    [
        ("http://0.0.0.0:8000", None),
        ("http://0.0.0.0:8000", "null"),
        ("http://0.0.0.0:8000", "https://evil.example"),
        ("http://0.0.0.0:8000", "http://0.0.0.0:8001"),
        ("http://0.0.0.0:8000", "https://0.0.0.0:8000"),
        ("http://verifair.internal", "http://verifair.internal"),
        ("http://127.evil.example", "http://127.evil.example"),
        ("http://192.168.1.10:8000", "http://192.168.1.10:8000"),
        ("http://[::]:8000", "http://[::]:8001"),
        ("http://localhost.:8000", "http://localhost.:8000"),
    ],
)
def test_web_post_without_sec_fetch_site_is_refused_unless_loopback_own_origin(
    settings, make_app, base_url, origin
):
    browser = TestClient(make_app(settings), base_url=base_url)
    headers = {"Origin": origin} if origin else {}
    r = browser.post("/me", data={"name": "Mallory"}, headers=headers, follow_redirects=False)
    if urlsplit(base_url).hostname in ("verifair.internal", "127.evil.example", "localhost."):
        # a dotted name that is not the configured address is refused before the browser check
        assert r.status_code == 400 and "Unknown host" in r.text
    else:
        assert (r.status_code, r.json()) == (403, {"detail": "browser request required"})


def test_loopback_own_origin_does_not_override_a_cross_site_header(settings, make_app):
    browser = TestClient(make_app(settings), base_url="http://0.0.0.0:8000")
    r = browser.post(
        "/me",
        data={"name": "Mallory"},
        headers={"Origin": "http://0.0.0.0:8000", "Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_shared_mode_has_no_loopback_origin_fallback(shared_settings, make_app):
    browser = TestClient(make_app(shared_settings), base_url="http://localhost:8000")
    r = browser.post(
        "/me",
        data={"name": "Mallory"},
        headers={"Origin": "http://localhost:8000"} | signed_in("admin@example.com"),
        follow_redirects=False,
    )
    assert (r.status_code, r.json()) == (403, {"detail": "browser request required"})


def test_api_post_is_exempt_from_origin_check(client):
    r = client.post("/api/reports", json=REPORT, headers=H | {"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 201


def test_cookies_are_secure_only_behind_https(new_database_url, make_app):
    for base_url, secure in [
        ("https://review.example.com", True),
        ("HTTPS://review.example.com", True),
        ("http://localhost", False),
    ]:
        app = make_app(Settings("t", new_database_url(), base_url, None))
        client = TestClient(
            app, base_url=base_url.lower(), headers={"Sec-Fetch-Site": "same-origin"}
        )
        client.post("/api/reports", json=REPORT, headers=H)
        page = client.get("/r/t").headers["set-cookie"]
        name = client.post("/me", data={"name": "A"}, follow_redirects=False).headers["set-cookie"]
        assert ("Secure" in page, "Secure" in name) == (secure, secure)


def test_html_responses_carry_csp(client):
    client.post("/api/reports", json=REPORT, headers=H)
    page = client.get("/r/t")
    assert page.headers["content-security-policy"] == (
        "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline';"
        " base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    assert "hx-on" not in page.text
    assert "content-security-policy" not in client.get("/healthz").headers
    for path in ["/r/t", "/healthz", "/static/app.js", "/api/reports/t"]:
        r = client.get(path, headers=H)
        assert r.headers["x-content-type-options"] == "nosniff", path
        assert r.headers["referrer-policy"] == "same-origin", path


def _seed_local_mode_ids(settings, make_app):
    # local mode stores ids as the agent or browser sent them
    local = TestClient(make_app(settings), headers={"Authorization": "Bearer t"})
    pushed = local.post(
        "/api/reports",
        json={
            "id": "old",
            "project": "p",
            "title": "Old change",
            "risk": "low",
            "body": "## A\nx",
            "author_id": "Jane@Example.com",
            "reviewers": ["Bob@Example.com", "CAROL@example.com", "carol@example.com"],
        },
    )
    assert pushed.status_code == 201
    local.app.state.pool.close()
    with closing(db.connect(settings.database_url)) as conn:
        db.push_report(conn, project="p", title="Legacy", risk="low", body="b", id="legacy")
        db.push_report(conn, project="p", title="Unowned", risk="low", body="b", id="unowned")
        for id in ("old", "legacy"):
            db.sign_version(
                conn, report_id=id, version=1, signer_id="Jane@Example.com", signer_name="J"
            )
        conn.execute("UPDATE reports SET author_id=NULL WHERE id='legacy'")
        db.add_verdict(
            conn,
            report_id="old",
            version=1,
            reviewer_id="JANE@example.com",
            reviewer_name="Jane",
            verdict="approve",
        )
        for author in ("Bob@Example.com", "agent", "Agent"):
            cid = db.add_comment(
                conn,
                report_id="old",
                version=1,
                author_id=author,
                author_name=author,
                body="Why?",
                section="A",
                blocking=False,
            )["id"]
            db.resolve_thread(conn, "old", cid, by=author)


def _stored_ids(url):
    queries = {
        "reports": "SELECT id, author_id FROM reports ORDER BY id",
        "signatures": "SELECT report_id, signer_id FROM signatures ORDER BY report_id",
        "verdicts": "SELECT reviewer_id FROM verdicts",
        "comments": "SELECT author_id, resolved_by FROM comments ORDER BY id",
        "requests": "SELECT report_id, reviewer_id FROM review_requests ORDER BY reviewer_id",
    }
    with closing(psycopg.connect(url)) as conn:
        return {name: conn.execute(sql).fetchall() for name, sql in queries.items()}


def test_shared_mode_startup_lower_cases_local_mode_ids_once(
    settings, shared_settings, make_app, caplog
):
    shared_settings = replace(shared_settings, database_url=settings.database_url)
    _seed_local_mode_ids(settings, make_app)
    caplog.set_level("INFO", logger="verifair")
    client = SessionClient(make_app(shared_settings), headers={"Sec-Fetch-Site": "same-origin"})
    assert _stored_ids(settings.database_url) == {
        "reports": [("legacy", None), ("old", "jane@example.com"), ("unowned", None)],
        "signatures": [("legacy", "jane@example.com"), ("old", "jane@example.com")],
        "verdicts": [("jane@example.com",)],
        "comments": [
            ("bob@example.com", "bob@example.com"),
            ("agent", "agent"),
            ("Agent", "Agent"),
        ],
        "requests": [("old", "bob@example.com"), ("old", "carol@example.com")],
    }
    assert "lowercased ids rows=8" in [r.getMessage() for r in caplog.records]

    with closing(db.connect(settings.database_url)) as conn:
        for id in ("jane@example.com", "bob@example.com"):
            db.add_user(conn, id=id, name=id.partition("@")[0], role="member")
        token, _ = db.create_token(conn, owner_id="jane@example.com", label="agent")
    jane = signed_in("jane@example.com")
    pushed = client.post(
        "/api/reports",
        json={"project": "p", "title": "Old change", "risk": "low", "body": "## A\ny", "id": "old"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert (pushed.status_code, pushed.json()["version"]) == (201, 2)
    assert "Old change" in client.get("/changes?scope=author", headers=jane).text
    assert client.post("/r/old/sign", data={"version": 2}, headers=jane).status_code == 200
    bob = client.get("/", headers=signed_in("bob@example.com"))
    assert "Old change" in bob.text

    before = _stored_ids(settings.database_url)
    caplog.clear()
    make_app(shared_settings)
    assert _stored_ids(settings.database_url) == before
    assert not [r for r in caplog.records if r.getMessage().startswith("lowercased")]


@pytest.fixture
def unreachable():
    swapped = []

    def swap(app):
        swapped.append((app, app.state.pool))
        app.state.pool = ConnectionPool(
            "host=127.0.0.1 port=1 connect_timeout=1", min_size=1, timeout=0.3, open=True
        )

    yield swap
    for app, real in swapped:
        app.state.pool.close()
        app.state.pool = real


def _assert_unavailable(response):
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.text == "The database is not available. Try again in a moment."


def test_api_answers_503_when_the_database_is_unreachable(client, unreachable, caplog):
    unreachable(client.app)
    caplog.set_level("ERROR", logger="verifair")
    _assert_unavailable(client.get("/api/reports/r1", headers={"Authorization": "Bearer t"}))
    assert [r.getMessage() for r in caplog.records] == [
        "database unavailable path='/api/reports/r1': PoolTimeout:"
        " couldn't get a connection after 0.30 sec"
    ]


def test_shared_mode_pages_answer_503_when_the_database_is_unreachable(shared_client, unreachable):
    unreachable(shared_client.app)
    _assert_unavailable(shared_client.get("/"))


def test_a_dropped_connection_mid_request_is_a_503(client, monkeypatch):
    def drop(*args, **kwargs):
        raise psycopg.errors.AdminShutdown("terminating connection due to administrator command")

    monkeypatch.setattr(db, "get_report", drop)
    _assert_unavailable(client.get("/api/reports/r1", headers={"Authorization": "Bearer t"}))


def test_other_database_errors_stay_500(client, monkeypatch):
    def cancel(*args, **kwargs):
        raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(db, "get_report", cancel)
    with pytest.raises(psycopg.errors.QueryCanceled):
        client.get("/api/reports/r1", headers={"Authorization": "Bearer t"})


def test_readyz_is_200_when_the_database_answers(client):
    response = client.get("/readyz")
    assert (response.status_code, response.json()) == (200, {"ok": True})


def test_readyz_is_public_in_shared_mode(shared_client):
    response = shared_client.get("/readyz", headers=signed_in(""))
    assert (response.status_code, response.json()) == (200, {"ok": True})


def test_readyz_answers_503_within_seconds_when_the_database_is_unreachable(client, unreachable):
    unreachable(client.app)
    started = time.monotonic()
    response = client.get("/readyz")
    assert (response.status_code, response.json()) == (503, {"ok": False})
    assert response.headers["Retry-After"] == "5"
    assert time.monotonic() - started < 3


def test_readyz_answers_503_when_no_connection_is_free(client, monkeypatch):
    monkeypatch.setattr(app_module, "READYZ_TIMEOUT", 0.3)
    held = client.app.state.pool.getconn()
    try:
        response = client.get("/readyz")
    finally:
        client.app.state.pool.putconn(held)
    assert (response.status_code, response.json()) == (503, {"ok": False})
    assert response.headers["Retry-After"] == "5"


LIMIT = 1024 * 1024
TOO_LARGE = {"detail": "request body too large"}


def _chunks(total):
    yield b"x" * total


@pytest.mark.parametrize("path", ["/api/reports", "/r/x/comments"])
def test_a_body_over_the_limit_is_413_with_content_length(client, path):
    response = client.post(path, content=b"x" * (LIMIT + 1), headers={"Authorization": "Bearer t"})
    assert (response.status_code, response.json()) == (413, TOO_LARGE)


@pytest.mark.parametrize(
    ("path", "content_type"),
    [("/api/reports", "application/json"), ("/r/x/comments", "application/x-www-form-urlencoded")],
)
def test_a_chunked_body_over_the_limit_is_413(client, path, content_type):
    headers = {"Authorization": "Bearer t", "Content-Type": content_type}
    response = client.post(path, content=_chunks(LIMIT + 1), headers=headers)
    assert "content-length" not in response.request.headers
    assert (response.status_code, response.json()) == (413, TOO_LARGE)


def test_a_body_of_exactly_the_limit_is_not_413(client):
    response = client.post(
        "/api/reports", content=b"x" * LIMIT, headers={"Authorization": "Bearer t"}
    )
    assert response.status_code == 422
    chunked = client.post(
        "/api/reports", content=_chunks(LIMIT), headers={"Authorization": "Bearer t"}
    )
    assert chunked.status_code == 422


def test_a_normal_push_still_works(client):
    payload = {"project": "p", "title": "T", "risk": "low", "body": "## Intent\nx"}
    assert (
        client.post("/api/reports", json=payload, headers={"Authorization": "Bearer t"}).status_code
        == 201
    )


@pytest.mark.parametrize("field", ["title", "risk", "links"])
@pytest.mark.parametrize(
    "container", [{"trace": {"output": "PRIVATE_TRACE"}}, [{"trace": {"output": "PRIVATE_TRACE"}}]]
)
def test_report_container_errors_never_echo_nested_trace(client, field, container):
    payload = {"project": "p", "title": "T", "risk": "low", "body": "B", field: container}
    response = client.post("/api/reports", json=payload, headers={"Authorization": "Bearer t"})
    assert response.status_code == 422
    assert "PRIVATE_TRACE" not in response.text
    assert all("input" not in error for error in response.json()["detail"])


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
def test_plan_counts_toward_request_body_limit(client, settings, chunked):
    payload = REPORT | {"body": "b" * (LIMIT // 2), "plan": "p" * (LIMIT // 2)}
    encoded = json.dumps(payload).encode()
    assert len(payload["body"].encode()) < LIMIT
    assert len(payload["plan"].encode()) < LIMIT
    assert len(encoded) > LIMIT
    response = client.post(
        "/api/reports",
        content=iter([encoded]) if chunked else encoded,
        headers=H | {"Content-Type": "application/json"},
    )
    assert ("content-length" not in response.request.headers) is chunked
    assert (response.status_code, response.json()) == (413, TOO_LARGE)
    with closing(db.connect(settings.database_url)) as conn:
        assert db.list_reports(conn) == []
    assert (
        client.post("/api/reports", json=REPORT | {"plan": "# Small"}, headers=H).status_code == 201
    )


@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
@pytest.mark.parametrize(
    "over_document_budget", [False, True], ids=["combined-1MiB", "trace-512KiB"]
)
def test_trace_and_combined_request_byte_limits(client, settings, chunked, over_document_budget):
    trace = {
        "schema_version": 1,
        "runs": [
            {
                "id": "run-1",
                "agent": "codex",
                "origin": "recorded",
                "source_format": "jsonl",
                "coverage": "partial",
                "warnings": [],
            }
        ],
        "events": [
            {
                "id": f"event-{i}",
                "run_id": "run-1",
                "ordinal": i + 1,
                "kind": "tool",
                "title": "check",
                "status": "unknown",
                "output": "é" * 16000,
            }
            for i in range(17 if over_document_budget else 8)
        ],
    }
    payload = REPORT | {
        "body": "small" if over_document_budget else "b" * 850000,
        "trace": trace,
    }
    encoded = json.dumps(payload, ensure_ascii=False).encode()
    assert (len(encoded) < LIMIT) is over_document_budget
    assert (len(json.dumps(trace, ensure_ascii=False).encode()) > 524288) is over_document_budget
    response = client.post(
        "/api/reports",
        content=iter([encoded[:300000], encoded[300000:]]) if chunked else encoded,
        headers=H | {"Content-Type": "application/json"},
    )
    assert ("content-length" not in response.request.headers) is chunked
    assert response.status_code == (422 if over_document_budget else 413)
    if over_document_budget:
        [error] = response.json()["detail"]
        assert error["loc"] == ["body", "trace"]
        assert "input" not in error and "ctx" not in error
    else:
        assert response.json() == TOO_LARGE
    with closing(db.connect(settings.database_url)) as conn:
        assert db.list_reports(conn) == []


def test_startup_config_error_is_one_line_and_exits_non_zero():
    env = {k: v for k, v in os.environ.items() if not k.startswith("VERIFAIR_")}
    result = subprocess.run(
        [sys.executable, "-c", "import verifair.asgi"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1 and "VERIFAIR_API_TOKEN is required in local mode." in lines[0]


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("localhost:8000", True),
        ("127.0.0.1:8000", True),
        ("[::1]:8000", True),
        ("192.168.1.20:8000", True),
        ("devbox:8000", True),
        ("app.localhost:8000", True),
        ("attacker.example:8000", False),
        ("evil.localhost.example.com", False),
    ],
)
def test_local_mode_refuses_hosts_a_rebinding_attack_could_use(client, host, allowed):
    r = client.get("/", headers={"Host": host})
    assert (r.status_code != 400) is allowed
    if not allowed:
        assert "VERIFAIR_BASE_URL" in r.text


def test_local_mode_accepts_its_configured_host(settings, make_app):
    app = make_app(replace(settings, base_url="http://verifair.lan:8000"))
    r = TestClient(app).get("/healthz", headers={"Host": "verifair.lan:8000"})
    assert r.status_code == 200


def test_shared_mode_does_not_check_the_host(shared_client):
    anonymous = TestClient(shared_client.app)
    assert anonymous.get("/healthz", headers={"Host": "10.0.0.5:8000"}).status_code == 200
    assert anonymous.get("/healthz", headers={"Host": "verifair.example.com"}).status_code == 200
