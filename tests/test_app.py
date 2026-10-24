from contextlib import closing
from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient

from verifair import db
from verifair.app import create_app
from verifair.config import Settings, load_settings


def test_health(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("VERIFAIR_API_TOKEN", raising=False)
    monkeypatch.delenv("VERIFAIR_IDENTITY_HEADER", raising=False)
    with pytest.raises(RuntimeError) as error:
        load_settings()
    assert str(error.value) == (
        "VERIFAIR_API_TOKEN is required in local mode."
        " Set VERIFAIR_IDENTITY_HEADER for shared mode."
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
        "This database has members. Set VERIFAIR_IDENTITY_HEADER to run in shared mode."
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


def test_shared_mode_loads_without_api_token(monkeypatch):
    monkeypatch.delenv("VERIFAIR_API_TOKEN", raising=False)
    monkeypatch.setenv("VERIFAIR_IDENTITY_HEADER", "X-Forwarded-Email")
    monkeypatch.setenv("VERIFAIR_BOOTSTRAP_ADMIN_ID", "admin@example.com")
    s = load_settings()
    assert (s.api_token, s.identity_header, s.bootstrap_admin_id) == (
        None,
        "X-Forwarded-Email",
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
    with pytest.raises(RuntimeError, match="plain id"):
        create_app(replace(shared_settings, bootstrap_admin_id=value))


def test_local_mode_startup_needs_no_users(settings, make_app):
    make_app(settings)
    with closing(db.connect(settings.database_url)) as conn:
        assert db.list_users(conn) == []


def test_bootstrap_admin_id_without_the_identity_header_refuses_to_start(settings):
    with pytest.raises(RuntimeError) as error:
        create_app(replace(settings, bootstrap_admin_id="admin@example.com"))
    assert str(error.value) == (
        "VERIFAIR_BOOTSTRAP_ADMIN_ID is set but VERIFAIR_IDENTITY_HEADER is not."
        " Set the header to run in shared mode."
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
    monkeypatch.delenv("VERIFAIR_API_TOKEN", raising=False)
    monkeypatch.setenv("VERIFAIR_DATABASE_URL", new_database_url())
    monkeypatch.setenv("VERIFAIR_IDENTITY_HEADER", "X-Forwarded-Email")
    monkeypatch.setenv("VERIFAIR_BOOTSTRAP_ADMIN_ID", " admin@example.com\n")
    s = load_settings()
    assert s.bootstrap_admin_id == "admin@example.com"
    client = TestClient(make_app(s), headers={"X-Forwarded-Email": "admin@example.com"})
    assert client.get("/access").status_code == 200


@pytest.mark.parametrize(
    "value", ["jane doe@example.com", "a/b", "a\\b", ".", "..", "a\nb", "agent"]
)
def test_bootstrap_admin_id_must_be_plain(shared_settings, value):
    with pytest.raises(RuntimeError) as error:
        create_app(replace(shared_settings, bootstrap_admin_id=value))
    assert str(error.value) == (
        "VERIFAIR_BOOTSTRAP_ADMIN_ID must be a plain id such as jane@example.com"
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


def test_api_post_is_exempt_from_origin_check(client):
    r = client.post("/api/reports", json=REPORT, headers=H | {"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 201


def test_cookies_are_secure_only_behind_https(new_database_url, make_app):
    for base_url, secure in [("https://review.example.com", True), ("http://localhost", False)]:
        app = make_app(Settings("t", new_database_url(), base_url, None))
        client = TestClient(app, base_url=base_url, headers={"Sec-Fetch-Site": "same-origin"})
        client.post("/api/reports", json=REPORT, headers=H)
        page = client.get("/r/t").headers["set-cookie"]
        name = client.post("/me", data={"name": "A"}, follow_redirects=False).headers["set-cookie"]
        assert ("Secure" in page, "Secure" in name) == (secure, secure)


def test_html_responses_carry_csp(client):
    client.post("/api/reports", json=REPORT, headers=H)
    page = client.get("/r/t")
    assert page.headers["content-security-policy"] == (
        "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline';"
        " frame-ancestors 'none'"
    )
    assert "hx-on" not in page.text
    assert "content-security-policy" not in client.get("/healthz").headers
    for path in ["/r/t", "/healthz", "/static/app.js", "/api/reports/t"]:
        r = client.get(path, headers=H)
        assert r.headers["x-content-type-options"] == "nosniff", path
