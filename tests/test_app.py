import json
import time
from contextlib import closing
from dataclasses import replace

import psycopg
import pytest
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
    client = TestClient(make_app(shared_settings), headers={"Sec-Fetch-Site": "same-origin"})
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
    jane = {"X-Forwarded-Email": "JANE@Example.COM"}
    pushed = client.post(
        "/api/reports",
        json={"project": "p", "title": "Old change", "risk": "low", "body": "## A\ny", "id": "old"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert (pushed.status_code, pushed.json()["version"]) == (201, 2)
    assert "Old change" in client.get("/changes?scope=author", headers=jane).text
    assert client.post("/r/old/sign", data={"version": 2}, headers=jane).status_code == 200
    bob = client.get("/", headers={"X-Forwarded-Email": "bob@example.com"})
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
    response = shared_client.get("/readyz", headers={"X-Forwarded-Email": ""})
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
