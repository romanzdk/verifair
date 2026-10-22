from dataclasses import replace

import pytest

from verifair import db
from verifair.app import create_app
from verifair.config import load_settings


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


def test_local_mode_refuses_a_database_with_members(settings):
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    db.add_user(conn, id="admin@example.com", name="admin", role="admin")
    conn.close()
    with pytest.raises(RuntimeError) as error:
        create_app(settings)
    assert str(error.value) == (
        "This database has members. Set VERIFAIR_IDENTITY_HEADER to run in shared mode."
    )


def test_startup_logs_the_bootstrap_outcome_and_an_ignored_shared_token(shared_settings, caplog):
    caplog.set_level("INFO", logger="verifair")
    create_app(shared_settings)
    create_app(replace(shared_settings, api_token="t"))
    create_app(replace(shared_settings, bootstrap_admin_id=None))
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


def test_shared_mode_startup_bootstraps_the_first_admin(shared_settings):
    def users():
        conn = db.connect(shared_settings.db_path)
        try:
            return [(u["id"], u["name"], u["role"]) for u in db.list_users(conn)]
        finally:
            conn.close()

    with pytest.raises(RuntimeError) as error:
        create_app(replace(shared_settings, bootstrap_admin_id=None))
    assert str(error.value) == "VERIFAIR_BOOTSTRAP_ADMIN_ID is required to create the first admin"
    assert users() == []
    create_app(shared_settings)
    assert users() == [("admin@example.com", "admin", "admin")]
    create_app(replace(shared_settings, bootstrap_admin_id=None))
    create_app(replace(shared_settings, bootstrap_admin_id="other@example.com"))
    assert users() == [("admin@example.com", "admin", "admin")]


def test_bootstrap_admin_id_is_stored_lower_case(shared_settings):
    create_app(replace(shared_settings, bootstrap_admin_id="Admin@Example.com"))
    conn = db.connect(shared_settings.db_path)
    assert [u["id"] for u in db.list_users(conn)] == ["admin@example.com"]
    conn.close()


@pytest.mark.parametrize("value", ["Agent", "AGENT"])
def test_bootstrap_admin_id_agent_is_refused_in_any_case(shared_settings, value):
    with pytest.raises(RuntimeError, match="plain id"):
        create_app(replace(shared_settings, bootstrap_admin_id=value))


def test_local_mode_startup_needs_no_users(settings):
    create_app(settings)
    conn = db.connect(settings.db_path)
    assert db.list_users(conn) == []
    conn.close()


def test_bootstrap_admin_id_without_the_identity_header_refuses_to_start(settings):
    with pytest.raises(RuntimeError) as error:
        create_app(replace(settings, bootstrap_admin_id="admin@example.com"))
    assert str(error.value) == (
        "VERIFAIR_BOOTSTRAP_ADMIN_ID is set but VERIFAIR_IDENTITY_HEADER is not."
        " Set the header to run in shared mode."
    )
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    assert db.list_users(conn) == []
    conn.close()


@pytest.mark.parametrize("value", ["", " \n"])
def test_empty_bootstrap_admin_id_is_none(monkeypatch, value):
    monkeypatch.setenv("VERIFAIR_API_TOKEN", "t")
    monkeypatch.setenv("VERIFAIR_BOOTSTRAP_ADMIN_ID", value)
    assert load_settings().bootstrap_admin_id is None


def test_bootstrap_admin_id_is_stripped(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    monkeypatch.delenv("VERIFAIR_API_TOKEN", raising=False)
    monkeypatch.setenv("VERIFAIR_DB_PATH", str(tmp_path / "strip.db"))
    monkeypatch.setenv("VERIFAIR_IDENTITY_HEADER", "X-Forwarded-Email")
    monkeypatch.setenv("VERIFAIR_BOOTSTRAP_ADMIN_ID", " admin@example.com\n")
    s = load_settings()
    assert s.bootstrap_admin_id == "admin@example.com"
    client = TestClient(create_app(s), headers={"X-Forwarded-Email": "admin@example.com"})
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
    assert (s.db_path, s.base_url, s.slack_webhook_url) == (
        "./verifair.db",
        "http://localhost:8000",
        None,
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


def test_web_post_without_sec_fetch_site_is_refused(settings):
    from fastapi.testclient import TestClient

    from verifair.app import create_app

    curl = TestClient(create_app(settings))
    r = curl.post("/me", data={"name": "Mallory"}, follow_redirects=False)
    assert r.status_code == 403
    assert r.json() == {"detail": "browser request required"}
    assert "vf_name" not in r.cookies


def test_api_post_is_exempt_from_origin_check(client):
    r = client.post("/api/reports", json=REPORT, headers=H | {"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 201


def test_cookies_are_secure_only_behind_https(tmp_path):
    from fastapi.testclient import TestClient

    from verifair.app import create_app
    from verifair.config import Settings

    for base_url, secure in [("https://review.example.com", True), ("http://localhost", False)]:
        app = create_app(Settings("t", str(tmp_path / f"{secure}.db"), base_url, None))
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


def _seed_mixed_case_ids(path):
    conn = db.connect(path)
    db.init_schema(conn)
    body = "## A\nx"
    db.push_report(
        conn,
        project="p",
        title="Old change",
        risk="low",
        body=body,
        id="old",
        author_id="Jane@Example.com",
        reviewers=["Bob@Example.com", "bob@example.com"],
    )
    db.push_report(conn, project="p", title="Legacy", risk="low", body=body, id="legacy")
    db.push_report(conn, project="p", title="Unowned", risk="low", body=body, id="unowned")
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
    conn.close()


def _ids(path):
    conn = db.connect(path)
    try:
        return {
            "reports": conn.execute("SELECT id, author_id FROM reports ORDER BY id").fetchall(),
            "signatures": conn.execute("SELECT report_id, signer_id FROM signatures").fetchall(),
            "verdicts": conn.execute("SELECT reviewer_id FROM verdicts").fetchall(),
            "comments": conn.execute("SELECT author_id, resolved_by FROM comments").fetchall(),
            "requests": conn.execute(
                "SELECT report_id, reviewer_id FROM review_requests"
            ).fetchall(),
        }
    finally:
        conn.close()


def _plain(ids):
    return {table: [tuple(row) for row in rows] for table, rows in ids.items()}


def test_shared_mode_startup_lower_cases_old_header_ids_once(shared_settings, caplog):
    from fastapi.testclient import TestClient

    caplog.set_level("INFO", logger="verifair")
    _seed_mixed_case_ids(shared_settings.db_path)
    client = TestClient(create_app(shared_settings), headers={"Sec-Fetch-Site": "same-origin"})
    assert _plain(_ids(shared_settings.db_path)) == {
        "reports": [
            ("legacy", "jane@example.com"),
            ("old", "jane@example.com"),
            ("unowned", None),
        ],
        "signatures": [("old", "jane@example.com"), ("legacy", "jane@example.com")],
        "verdicts": [("jane@example.com",)],
        "comments": [
            ("bob@example.com", "bob@example.com"),
            ("agent", "agent"),
            ("Agent", "Agent"),
        ],
        "requests": [("old", "bob@example.com")],
    }
    assert "lowercased ids values=8" in [r.getMessage() for r in caplog.records]

    conn = db.connect(shared_settings.db_path)
    for id in ("jane@example.com", "bob@example.com"):
        db.add_user(conn, id=id, name=id.partition("@")[0], role="member")
    token, _ = db.create_token(conn, owner_id="jane@example.com", label="agent")
    conn.close()
    bearer = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/reports/old", headers=bearer).json()["status"] == "awaiting_review"
    r = client.post(
        "/r/old/verdict",
        data={"version": 1, "verdict": "approve"},
        headers={"X-Forwarded-Email": "JANE@Example.COM"},
    )
    assert "You signed this report. Someone else has to approve it." in r.text
    assert len(client.get("/api/reports/old", headers=bearer).json()["verdicts"]) == 1

    pushed = client.post(
        "/api/reports",
        json={"project": "p", "title": "Old change", "risk": "low", "body": "## A\ny", "id": "old"},
        headers=bearer,
    )
    assert (pushed.status_code, pushed.json()["version"]) == (201, 2)

    mine = client.get("/?scope=reviewer", headers={"X-Forwarded-Email": "bob@example.com"})
    assert "Old change" in mine.text

    before = _plain(_ids(shared_settings.db_path))
    caplog.clear()
    create_app(shared_settings)
    assert _plain(_ids(shared_settings.db_path)) == before
    assert not [r for r in caplog.records if r.getMessage().startswith("lowercased")]
    conn = db.connect(shared_settings.db_path)
    assert db.lowercase_human_ids(conn) == 0
    conn.close()


def test_lowercasing_reads_no_rows_when_nothing_is_left_to_change(shared_settings):
    _seed_mixed_case_ids(shared_settings.db_path)
    conn = db.connect(shared_settings.db_path)
    db.lowercase_human_ids(conn)
    statements = []
    conn.set_trace_callback(statements.append)
    changes = conn.total_changes
    assert db.lowercase_human_ids(conn) == 0
    conn.set_trace_callback(None)
    selects = [s for s in statements if s.startswith("SELECT")]
    assert len(selects) == len(db.HUMAN_ID_COLUMNS)
    assert all(conn.execute(s).fetchall() == [] for s in selects)
    assert conn.total_changes == changes
    conn.close()


def test_refused_shared_start_leaves_stored_ids_alone(shared_settings):
    _seed_mixed_case_ids(shared_settings.db_path)
    conn = db.connect(shared_settings.db_path)
    db.init_schema(conn)
    conn.close()
    before = _plain(_ids(shared_settings.db_path))
    assert before["verdicts"] == [("JANE@example.com",)]
    with pytest.raises(RuntimeError, match="VERIFAIR_BOOTSTRAP_ADMIN_ID is required"):
        create_app(replace(shared_settings, bootstrap_admin_id=None))
    assert _plain(_ids(shared_settings.db_path)) == before


def test_local_mode_startup_keeps_mixed_case_cookie_ids(settings):
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    db.push_report(
        conn, project="p", title="T", risk="low", body="## A\nx", id="r", author_id="AbCdEfGh12"
    )
    db.sign_version(conn, report_id="r", version=1, signer_id="AbCdEfGh12", signer_name="A")
    db.add_verdict(
        conn, report_id="r", version=1, reviewer_id="XyZxYz99", reviewer_name="X", verdict="approve"
    )
    cid = db.add_comment(
        conn, report_id="r", version=1, author_id="XyZxYz99", author_name="X", body="b"
    )["id"]
    db.resolve_thread(conn, "r", cid, by="XyZxYz99")
    conn.close()
    before = _plain(_ids(settings.db_path))
    create_app(settings)
    assert _plain(_ids(settings.db_path)) == before
    assert before["reports"] == [("r", "AbCdEfGh12")]
