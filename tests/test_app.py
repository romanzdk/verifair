import pytest

from verifair.config import load_settings


def test_health(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_missing_token_raises(monkeypatch):
    monkeypatch.delenv("VERIFAIR_API_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="VERIFAIR_API_TOKEN"):
        load_settings()


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
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
    ],
)
def test_cross_origin_web_post_is_refused(client, headers):
    r = client.post("/me", data={"name": "Mallory"}, headers=headers, follow_redirects=False)
    assert r.status_code == 403
    assert "vf_name" not in r.cookies


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "none"},
        {"Origin": "http://testserver"},
    ],
)
def test_same_origin_web_post_is_allowed(client, headers):
    r = client.post("/me", data={"name": "Alice"}, headers=headers, follow_redirects=False)
    assert r.status_code == 303


def test_api_post_is_exempt_from_origin_check(client):
    r = client.post("/api/reports", json=REPORT, headers=H | {"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 201


def test_cookies_are_secure_only_behind_https(tmp_path):
    from fastapi.testclient import TestClient

    from verifair.app import create_app
    from verifair.config import Settings

    for base_url, secure in [("https://review.example.com", True), ("http://localhost", False)]:
        app = create_app(Settings("t", str(tmp_path / f"{secure}.db"), base_url, None))
        client = TestClient(app, base_url=base_url)
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
