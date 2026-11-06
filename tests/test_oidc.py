import time
from contextlib import closing
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg
import pytest
from conftest import CLIENT_ID, ISSUER, SESSION_SECRET, signed_in
from fastapi.testclient import TestClient

from verifair import auth, db
from verifair.app import create_app
from verifair.config import load_settings

FAILED = "<h1>Sign-in failed</h1>"
WEEK = 7 * 24 * 3600


@pytest.fixture
def anonymous(shared_client):
    return TestClient(shared_client.app, headers={"Sec-Fetch-Site": "same-origin"})


def _set_cookies(response) -> dict[str, str]:
    return {h.split("=", 1)[0]: h for h in response.headers.get_list("set-cookie")}


def test_login_redirects_to_the_provider_with_state_nonce_and_pkce(anonymous):
    r = anonymous.get("/login", params={"next": "/r/x"}, follow_redirects=False)
    assert r.status_code == 303
    url = urlsplit(r.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/authorize"
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == "http://localhost:8000/auth/callback"
    assert query["scope"] == "openid email profile"
    assert query["code_challenge_method"] == "S256"
    assert len(query["state"]) >= 32 and len(query["nonce"]) >= 32
    assert len(query["code_challenge"]) == 43
    flow = _set_cookies(r)[auth.FLOW_COOKIE].lower()
    assert "httponly" in flow and "samesite=lax" in flow and "path=/" in flow
    assert "max-age=600" in flow and "secure" not in flow
    again = anonymous.get("/login", follow_redirects=False)
    assert parse_qs(urlsplit(again.headers["location"]).query)["state"][0] != query["state"]


def test_sign_in_sets_a_session_and_returns_to_next(anonymous, provider):
    r = provider.sign_in(anonymous, next="/changes?scope=author", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/changes?scope=author")
    cookies = _set_cookies(r)
    session = cookies[auth.SESSION_COOKIE].lower()
    assert "httponly" in session and "samesite=lax" in session and "path=/" in session
    assert f"max-age={WEEK}" in session and "secure" not in session
    assert "max-age=0" in cookies[auth.FLOW_COOKIE].lower()
    token_call = provider.calls[-1]
    form = {k: v[0] for k, v in parse_qs(token_call.content.decode()).items()}
    assert form["grant_type"] == "authorization_code"
    assert form["client_id"] == CLIENT_ID and form["client_secret"] == "client-secret"
    assert form["redirect_uri"] == "http://localhost:8000/auth/callback"
    assert "code_verifier" in form
    page = anonymous.get("/")
    assert page.status_code == 200 and '<span class="me" title="admin">admin</span>' in page.text
    user = page.text.split('<div class="app-user">')[1]
    order = (
        'class="me-row"',
        'class="me-id"',
        '<form method="post" action="/logout" class="signout"',
    )
    assert [user.index(part) for part in order] == sorted(user.index(part) for part in order)
    assert 'class="signout" aria-label="Sign out"' in user


def test_member_id_is_the_lower_cased_email(anonymous, provider):
    r = provider.sign_in(anonymous, email="Admin@Example.COM", follow_redirects=False)
    assert r.status_code == 303
    assert anonymous.get("/access/members").status_code == 200


def test_discovery_is_cached(anonymous, provider):
    provider.sign_in(anonymous)
    provider.sign_in(anonymous)
    paths = [c.url.path for c in provider.calls]
    assert paths.count("/.well-known/openid-configuration") == 1


def test_token_endpoint_uses_basic_auth_when_post_is_not_offered(anonymous, provider):
    provider.discovery["token_endpoint_auth_methods_supported"] = ["client_secret_basic"]
    assert provider.sign_in(anonymous, follow_redirects=False).status_code == 303
    token_call = provider.calls[-1]
    assert "client_secret" not in token_call.content.decode()
    assert (
        httpx.BasicAuth(CLIENT_ID, "client-secret")._auth_header
        == (token_call.headers["authorization"])
    )


def _fails(r, reason, caplog):
    assert r.status_code == 400 and FAILED in r.text
    assert auth.SESSION_COOKIE not in _set_cookies(r)
    assert f"sign-in failed reason={reason}" in caplog.text


def test_state_mismatch_fails(anonymous, provider, caplog):
    caplog.set_level("INFO", logger="verifair")
    params = provider.authorize(anonymous) | {"state": "wrong"}
    _fails(anonymous.get("/auth/callback", params=params), "state", caplog)


def test_callback_without_the_flow_cookie_fails(anonymous, shared_client, provider, caplog):
    caplog.set_level("INFO", logger="verifair")
    params = provider.authorize(anonymous)
    fresh = TestClient(shared_client.app)
    _fails(fresh.get("/auth/callback", params=params), "no-flow", caplog)


def test_expired_flow_cookie_fails(anonymous, provider, caplog, monkeypatch):
    caplog.set_level("INFO", logger="verifair")
    params = provider.authorize(anonymous)
    later = time.time() + 601
    monkeypatch.setattr(auth.time, "time", lambda: later)
    _fails(anonymous.get("/auth/callback", params=params), "no-flow", caplog)


def test_pkce_mismatch_fails(anonymous, provider, caplog):
    caplog.set_level("INFO", logger="verifair")
    params = provider.authorize(anonymous)
    provider.challenges[params["code"]] = ("not-the-challenge", "")
    _fails(anonymous.get("/auth/callback", params=params), "token-endpoint", caplog)


def test_provider_error_fails(anonymous, provider, caplog):
    caplog.set_level("INFO", logger="verifair")
    params = provider.authorize(anonymous)
    r = anonymous.get("/auth/callback", params={"state": params["state"], "error": "denied"})
    _fails(r, "provider-error", caplog)
    assert 'href="/login"' in r.text


def _now_relative(claims):
    # exp and iat are offsets so they are taken when the test runs, not at collection
    now = int(time.time())
    return {k: now + v if k in ("exp", "iat") and type(v) is int else v for k, v in claims.items()}


@pytest.mark.parametrize(
    ("claims", "reason"),
    [
        ({"nonce": "other"}, "nonce"),
        ({"nonce": None}, "nonce"),
        ({"iss": "https://evil.example.com"}, "iss"),
        ({"aud": "other-client"}, "aud"),
        ({"aud": ["other-client", CLIENT_ID]}, "aud"),
        ({"aud": [CLIENT_ID, "other"], "azp": "other"}, "aud"),
        ({"azp": "other"}, "aud"),
        ({"exp": -200}, "exp"),
        ({"exp": None}, "exp"),
        ({"iat": 300}, "iat"),
        ({"iat": "now"}, "iat"),
        ({"email_verified": False}, "email"),
        ({"email_verified": "true"}, "email"),
        ({"email_verified": None}, "email"),
        ({"email": None}, "email"),
        ({"email": "no-at-sign"}, "email"),
        ({"email": "a b@example.com"}, "email"),
    ],
)
def test_bad_id_token_claims_fail(anonymous, provider, caplog, claims, reason):
    caplog.set_level("INFO", logger="verifair")
    _fails(provider.sign_in(anonymous, **_now_relative(claims)), reason, caplog)


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": ISSUER + "/"},
        {"aud": [CLIENT_ID]},
        {"aud": [CLIENT_ID, "other"], "azp": CLIENT_ID},
        {"exp": -60},
        {"iat": 60},
    ],
)
def test_id_token_edge_cases_that_pass(anonymous, provider, claims):
    response = provider.sign_in(anonymous, follow_redirects=False, **_now_relative(claims))
    assert response.status_code == 303


def test_malformed_id_token_fails(anonymous, provider, caplog, monkeypatch):
    caplog.set_level("INFO", logger="verifair")
    handler = provider.handler

    def broken(request):
        response = handler(request)
        if request.url.path == "/token":
            return httpx.Response(200, json={"id_token": "not.a-jwt"})
        return response

    anonymous.app.state.oidc_transport = httpx.MockTransport(broken)
    _fails(provider.sign_in(anonymous), "id-token", caplog)


@pytest.fixture
def domain_client(shared_settings, make_app, provider):
    def make(issuer=ISSUER, domains=("example.com",), **extra):
        provider.discovery |= {"issuer": issuer}
        settings = replace(shared_settings, oidc_issuer=issuer, oidc_allowed_domains=domains)
        app = make_app(replace(settings, **extra))
        app.state.oidc_transport = httpx.MockTransport(provider.handler)
        return TestClient(app)

    return make


def test_allowed_domains_check_the_email_and_hd(domain_client, provider, caplog):
    caplog.set_level("INFO", logger="verifair")
    client = domain_client()
    _fails(provider.sign_in(client, email="admin@evil.com"), "domain", caplog)
    _fails(provider.sign_in(client, email="admin@sub.example.com"), "domain", caplog)
    _fails(provider.sign_in(client, hd="evil.com"), "domain", caplog)
    assert provider.sign_in(client, follow_redirects=False).status_code == 303
    assert provider.sign_in(client, hd="Example.com", follow_redirects=False).status_code == 303


def test_google_with_allowed_domains_needs_hd(domain_client, provider, caplog):
    caplog.set_level("INFO", logger="verifair")
    google = "https://accounts.google.com"
    client = domain_client(issuer=google)
    _fails(provider.sign_in(client, iss=google), "domain", caplog)
    ok = provider.sign_in(client, iss=google, hd="example.com", follow_redirects=False)
    assert ok.status_code == 303


def test_without_allowed_domains_any_domain_signs_in(domain_client, provider):
    client = domain_client(domains=())
    r = provider.sign_in(client, email="x@other.org", hd="other.org", follow_redirects=False)
    assert r.status_code == 403 and "Ask an admin to add x@other.org." in r.text


def test_open_signup_makes_new_people_from_allowed_domains_members(
    domain_client, provider, shared_client
):
    client = domain_client(open_signup=True)
    r = provider.sign_in(
        client, email="New@Example.com", sub="new-1", name="New  Person", follow_redirects=False
    )
    assert r.status_code == 303
    with closing(db.connect(client.app.state.settings.database_url)) as conn:
        user = db.get_user(conn, "new@example.com")
    assert (user["role"], user["name"], user["active"]) == ("member", "New Person", 1)
    assert client.get("/").status_code == 200
    assert client.get("/access/members").status_code == 403
    client.cookies.clear()
    provider.sign_in(client)
    note = "Open sign-up is on: anyone with an email at example.com becomes a member"
    assert note in client.get("/access/members").text
    assert note not in shared_client.get("/access/members").text


def test_open_signup_keeps_deactivated_members_out(domain_client, provider):
    client = domain_client(open_signup=True)
    with closing(db.connect(client.app.state.settings.database_url)) as conn:
        db.add_user(conn, id="gone@example.com", name="Gone", role="member")
        db.update_user(conn, "gone@example.com", active=False)
    r = provider.sign_in(client, email="gone@example.com", follow_redirects=False)
    assert r.status_code == 403


@pytest.mark.parametrize(
    "next",
    [
        "//evil.com",
        "/\\evil.com",
        "/x\\y",
        "https://evil.com",
        "evil.com",
        "/\t/evil.com",
        "/\n/x",
        "",
        "/" + "a" * 3000,
    ],
)
def test_unsafe_next_falls_back_to_the_inbox(anonymous, provider, next):
    r = provider.sign_in(anonymous, next=next, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/")


def test_safe_next_is_kept(anonymous, provider):
    r = provider.sign_in(anonymous, next="/r/x/v/2?tab=all#t", follow_redirects=False)
    assert r.headers["location"] == "/r/x/v/2?tab=all#t"


def test_discovery_with_another_issuer_is_refused(anonymous, provider, caplog):
    caplog.set_level("INFO", logger="verifair")
    provider.discovery["issuer"] = "https://evil.example.com"
    r = anonymous.get("/login", follow_redirects=False)
    assert r.status_code == 502 and FAILED in r.text
    assert "sign-in failed reason=discovery" in caplog.text


def test_discovery_with_plain_http_endpoints_is_refused(anonymous, provider):
    provider.discovery["token_endpoint"] = "http://idp.example.com/token"
    assert anonymous.get("/login", follow_redirects=False).status_code == 502


def test_unreachable_provider_is_a_502(anonymous):
    def down(request):
        raise httpx.ConnectError("refused")

    anonymous.app.state.oidc_transport = httpx.MockTransport(down)
    assert anonymous.get("/login", follow_redirects=False).status_code == 502


def test_https_base_url_marks_cookies_secure(shared_settings, make_app, provider):
    settings = replace(shared_settings, base_url="https://verifair.example.com")
    app = make_app(settings)
    app.state.oidc_transport = httpx.MockTransport(provider.handler)
    client = TestClient(app, base_url="https://verifair.example.com")
    r = provider.sign_in(client, follow_redirects=False)
    assert "secure" in _set_cookies(r)["__Host-vf_session"].lower()
    query = parse_qs(
        urlsplit(client.get("/login", follow_redirects=False).headers["location"]).query
    )
    assert query["redirect_uri"] == ["https://verifair.example.com/auth/callback"]


def test_no_session_redirects_pages_and_refuses_htmx_and_posts(anonymous):
    r = anonymous.get("/r/x", params={"tab": "a b"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/login?next=%2Fr%2Fx%3Ftab%3Da%2Bb")
    htmx = anonymous.get("/r/x", headers={"HX-Request": "true"})
    assert htmx.status_code == 401
    post = anonymous.post("/r/x/comments", data={"body": "x"})
    assert post.status_code == 401 and "Sign in" in post.text


def test_session_cookie_checks(anonymous):
    now = time.time()
    good = auth.make_session(SESSION_SECRET, "admin@example.com")
    body, _, mac = good.partition(".")
    other_body = auth.make_session(SESSION_SECRET, "bob@example.com").partition(".")[0]
    flow = auth.sign(SESSION_SECRET, "flow", {"sub": "admin@example.com", "iat": now})
    bad = [
        f"{other_body}.{mac}",
        good + "x",
        body,
        "",
        "...",
        "é.é",
        auth.make_session("another-secret-of-at-least-32-chars!", "admin@example.com"),
        auth.make_session(SESSION_SECRET, "admin@example.com", now=now - WEEK - 1),
        auth.make_session(SESSION_SECRET, "admin@example.com", now=now + 600),
        auth.sign(SESSION_SECRET, "session", {"sub": "admin@example.com"}),
        auth.sign(SESSION_SECRET, "session", ["admin@example.com"]),
        flow,
    ]
    for value in bad:
        assert auth.read_session(SESSION_SECRET, value) is None, value
        if not value.isascii():
            continue
        cookie = {"Cookie": f"{auth.SESSION_COOKIE}={value}"}
        r = anonymous.get("/", headers=cookie, follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, "/login?next=%2F"), value
    assert auth.read_session(SESSION_SECRET, good)[0] == "admin@example.com"
    old = auth.make_session(SESSION_SECRET, "admin@example.com", now=now - WEEK + 60)
    assert auth.read_session(SESSION_SECRET, old)[0] == "admin@example.com"


def test_session_denials_are_logged(anonymous, caplog):
    caplog.set_level("INFO", logger="verifair")
    anonymous.get("/r/x", follow_redirects=False)
    anonymous.get("/r/x", headers={"Cookie": f"{auth.SESSION_COOKIE}=x.y"}, follow_redirects=False)
    assert "membership denied reason=no-session path='/r/x'" in caplog.text
    assert "membership denied reason=bad-session path='/r/x'" in caplog.text


def test_non_member_and_deactivated_member_see_403_with_sign_out(
    shared_client, shared_settings, as_user
):
    as_user("gone@example.com")
    with closing(db.connect(shared_settings.database_url)) as conn:
        db.update_user(conn, "gone@example.com", active=False)
    for who in ("stranger@example.com", "gone@example.com"):
        r = shared_client.get("/", headers=signed_in(who))
        assert r.status_code == 403 and who in r.text
        assert '<form method="post" action="/logout" aria-label="Sign out">' in r.text
        assert "<button" in r.text and "Sign out" in r.text


def test_logout_clears_the_session(shared_client, as_user):
    r = shared_client.post("/logout", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/signed-out")
    cleared = _set_cookies(r)[auth.SESSION_COOKIE].lower()
    assert "max-age=0" in cleared and "path=/" in cleared
    page = TestClient(shared_client.app).get("/signed-out")
    assert page.status_code == 200 and "You signed out." in page.text
    assert '<a class="brand" href="/">' in page.text
    assert 'href="/login"' in page.text
    stranger = shared_client.post(
        "/logout", headers=signed_in("stranger@example.com"), follow_redirects=False
    )
    assert stranger.status_code == 303


def test_logout_ends_a_copied_session_cookie_too(shared_client, as_user):
    bob = as_user("bob@example.com")
    assert shared_client.get("/", headers=bob).status_code == 200
    shared_client.post("/logout", headers=bob, follow_redirects=False)
    r = shared_client.get("/", headers=bob, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert shared_client.get("/", headers=signed_in("bob@example.com")).status_code == 200
    assert shared_client.get("/").status_code == 200


def test_logout_still_clears_the_cookie_when_the_database_fails(shared_client, monkeypatch):
    def down(conn, id):
        raise psycopg.OperationalError("down")

    monkeypatch.setattr(db, "end_sessions", down)
    r = shared_client.post("/logout", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/signed-out")
    assert "max-age=0" in _set_cookies(r)[auth.SESSION_COOKIE].lower()


def test_logout_needs_a_same_origin_browser_request(shared_client):
    r = shared_client.post("/logout", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403 and auth.SESSION_COOKIE not in _set_cookies(r)
    bare = TestClient(shared_client.app).post("/logout")
    assert bare.status_code == 403


def test_sign_in_routes_do_not_exist_in_local_mode(client):
    for path in ("/login", "/auth/callback", "/signed-out"):
        assert client.get(path).status_code == 404
    assert client.post("/logout").status_code == 404


def _env(monkeypatch, **values):
    for key in (
        "VERIFAIR_API_TOKEN",
        "VERIFAIR_IDENTITY_HEADER",
        "VERIFAIR_OIDC_ISSUER",
        "VERIFAIR_OIDC_CLIENT_ID",
        "VERIFAIR_OIDC_CLIENT_SECRET",
        "VERIFAIR_SESSION_SECRET",
        "VERIFAIR_OIDC_ALLOWED_DOMAINS",
        "VERIFAIR_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(f"VERIFAIR_{key}", value)


def test_identity_header_is_refused_at_startup(monkeypatch):
    _env(monkeypatch, API_TOKEN="t", IDENTITY_HEADER="X-Forwarded-Email")
    with pytest.raises(RuntimeError) as error:
        load_settings()
    assert str(error.value) == (
        "VERIFAIR_IDENTITY_HEADER was removed. verifair now signs people in itself:"
        " set VERIFAIR_OIDC_ISSUER, VERIFAIR_OIDC_CLIENT_ID, VERIFAIR_OIDC_CLIENT_SECRET"
        " and VERIFAIR_SESSION_SECRET instead. See docs/access.md."
    )


def test_oidc_settings_load_from_the_environment(monkeypatch):
    _env(
        monkeypatch,
        OIDC_ISSUER=" https://accounts.google.com ",
        OIDC_CLIENT_ID="id",
        OIDC_CLIENT_SECRET="secret",
        SESSION_SECRET="x" * 32,
        OIDC_ALLOWED_DOMAINS=" Example.com, , corp.example.org ",
    )
    s = load_settings()
    assert (s.shared, s.api_token, s.oidc_issuer) == (True, None, "https://accounts.google.com")
    assert (s.oidc_client_id, s.oidc_client_secret, s.session_secret) == ("id", "secret", "x" * 32)
    assert s.oidc_allowed_domains == ("example.com", "corp.example.org")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"oidc_client_id": None},
            "VERIFAIR_OIDC_CLIENT_ID is required when VERIFAIR_OIDC_ISSUER is set.",
        ),
        (
            {"oidc_client_secret": None, "session_secret": None},
            "VERIFAIR_OIDC_CLIENT_SECRET, VERIFAIR_SESSION_SECRET are required"
            " when VERIFAIR_OIDC_ISSUER is set.",
        ),
        (
            {"session_secret": "s" * 31},
            "VERIFAIR_SESSION_SECRET must be at least 32 characters."
            " Create one with: openssl rand -base64 48",
        ),
        (
            {"oidc_issuer": "http://idp.example.com"},
            "VERIFAIR_OIDC_ISSUER must be an https URL such as https://accounts.google.com",
        ),
        (
            {"base_url": "http://verifair.example.com"},
            "VERIFAIR_BASE_URL must be the https URL people open, such as"
            " https://verifair.example.com. Sign-in needs it for the redirect URI.",
        ),
    ],
)
def test_incomplete_oidc_settings_refuse_to_start(shared_settings, change, message):
    with pytest.raises(RuntimeError) as error:
        create_app(replace(shared_settings, **change))
    assert str(error.value) == message
    assert "s" * 31 not in str(error.value)


@pytest.mark.parametrize("issuer", ["http://localhost:8080/realms/x", "http://127.0.0.1:9000"])
def test_local_http_issuer_is_allowed(shared_settings, make_app, issuer):
    make_app(replace(shared_settings, oidc_issuer=issuer))


def _https_client(shared_settings, make_app, provider, base_url="https://verifair.example.com"):
    app = make_app(replace(shared_settings, base_url=base_url))
    app.state.oidc_transport = httpx.MockTransport(provider.handler)
    return TestClient(
        app, base_url="https://verifair.example.com", headers={"Sec-Fetch-Site": "same-origin"}
    )


@pytest.mark.parametrize(
    "base_url", ["https://verifair.example.com", "HTTPS://verifair.example.com"]
)
def test_https_cookies_use_the_host_prefix(shared_settings, make_app, provider, base_url):
    client = _https_client(shared_settings, make_app, provider, base_url)
    login = client.get("/login", follow_redirects=False)
    flow = _set_cookies(login)["__Host-vf_signin"].lower()
    assert "secure" in flow and "path=/;" in flow + ";" and "domain" not in flow
    client.cookies.clear()
    r = provider.sign_in(client, follow_redirects=False)
    session = _set_cookies(r)["__Host-vf_session"].lower()
    assert "secure" in session and "path=/" in session and "domain" not in session
    assert "max-age=0" in _set_cookies(r)["__Host-vf_signin"].lower()
    assert client.get("/", follow_redirects=False).status_code == 200
    plain = {"Cookie": f"vf_session={auth.make_session(SESSION_SECRET, 'admin@example.com')}"}
    fresh = TestClient(client.app, base_url="https://verifair.example.com")
    assert fresh.get("/", headers=plain, follow_redirects=False).status_code == 303
    out = client.post("/logout", follow_redirects=False)
    assert "max-age=0" in _set_cookies(out)["__Host-vf_session"].lower()


def test_google_without_allowed_domains_refuses_to_start(shared_settings):
    for issuer in ("https://accounts.google.com", "https://accounts.google.com/"):
        with pytest.raises(RuntimeError) as error:
            create_app(replace(shared_settings, oidc_issuer=issuer))
        assert "VERIFAIR_OIDC_ALLOWED_DOMAINS is required" in str(error.value)


def test_open_signup_without_allowed_domains_refuses_to_start(shared_settings):
    with pytest.raises(RuntimeError) as error:
        create_app(replace(shared_settings, open_signup=True))
    assert "VERIFAIR_OPEN_SIGNUP needs VERIFAIR_OIDC_ALLOWED_DOMAINS" in str(error.value)


@pytest.mark.parametrize(("raw", "on"), [("", False), ("0", False), ("1", True), ("true", True)])
def test_open_signup_setting(monkeypatch, raw, on):
    monkeypatch.setenv("VERIFAIR_API_TOKEN", "t")
    monkeypatch.setenv("VERIFAIR_DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("VERIFAIR_OPEN_SIGNUP", raw)
    assert load_settings().open_signup is on


def test_google_accepts_the_issuer_without_https(domain_client, provider):
    google = "https://accounts.google.com"
    client = domain_client(issuer=google)
    for iss in ("accounts.google.com", google):
        r = provider.sign_in(client, iss=iss, hd="example.com", follow_redirects=False)
        assert r.status_code == 303, iss


def test_other_issuers_do_not_accept_a_bare_host(anonymous, provider, caplog):
    caplog.set_level("INFO", logger="verifair")
    _fails(provider.sign_in(anonymous, iss="idp.example.com"), "iss", caplog)


def test_non_ascii_email_fails(anonymous, provider, caplog):
    caplog.set_level("INFO", logger="verifair")
    _fails(provider.sign_in(anonymous, email="admín@example.com"), "email", caplog)


def test_settings_repr_hides_secrets(shared_settings):
    settings = replace(shared_settings, api_token="tok-secret", smtp_url="smtp://u:pw-secret@h:587")
    text = repr(settings)
    for secret in ("client-secret", SESSION_SECRET, "tok-secret", "pw-secret"):
        assert secret not in text
    assert ISSUER in text


ACCOUNT = "This email is linked to a different account. Ask an admin to reset your sign-in."


def _user(app, id):
    with closing(db.connect(app.state.settings.database_url)) as conn:
        return db.get_user(conn, id)


def test_first_sign_in_links_the_account_and_another_account_is_refused(
    anonymous, provider, caplog
):
    caplog.set_level("INFO", logger="verifair")
    assert provider.sign_in(anonymous, follow_redirects=False).status_code == 303
    assert _user(anonymous.app, "admin@example.com")["oidc_sub"] == "123"
    assert provider.sign_in(anonymous, follow_redirects=False).status_code == 303
    anonymous.cookies.clear()
    r = provider.sign_in(anonymous, sub="456", email="Admin@example.com")
    _fails(r, "account", caplog)
    assert ACCOUNT in r.text
    assert _user(anonymous.app, "admin@example.com")["oidc_sub"] == "123"


def test_sign_in_takes_the_provider_name_only_over_the_default(anonymous, provider):
    url = anonymous.app.state.settings.database_url
    assert _user(anonymous.app, "admin@example.com")["name"] == "admin"
    provider.sign_in(anonymous, name="   ")
    assert _user(anonymous.app, "admin@example.com")["name"] == "admin"
    provider.sign_in(anonymous, name="  Ada\n Lovelace ")
    assert _user(anonymous.app, "admin@example.com")["name"] == "Ada Lovelace"
    with closing(db.connect(url)) as conn:
        db.add_user(conn, id="bob@example.com", name="bob", role="member")
    anonymous.cookies.clear()
    provider.sign_in(anonymous, email="bob@example.com", sub="bob-sub", name="Alice Admin")
    assert _user(anonymous.app, "bob@example.com")["name"] == "bob"


def test_provider_name_needs_the_linked_account_and_plain_text(anonymous, provider):
    provider.sign_in(anonymous)
    anonymous.cookies.clear()
    provider.sign_in(anonymous, sub="someone-else", name="Mallory")
    assert _user(anonymous.app, "admin@example.com")["name"] == "admin"
    anonymous.cookies.clear()
    provider.sign_in(anonymous, name="\u202eAda\u200b \x07Lovelace\u2066")
    assert _user(anonymous.app, "admin@example.com")["name"] == "Ada Lovelace"


def test_long_provider_name_is_cut_without_a_trailing_space(anonymous, provider):
    provider.sign_in(anonymous, name="A" * 59 + " Smythe")
    assert _user(anonymous.app, "admin@example.com")["name"] == "A" * 59


def test_deactivated_member_is_never_linked_or_signed_in(anonymous, provider, caplog):
    caplog.set_level("INFO", logger="verifair")
    url = anonymous.app.state.settings.database_url
    with closing(db.connect(url)) as conn:
        db.add_user(conn, id="bob@example.com", name="Bob", role="member")
        assert db.link_account(conn, "bob@example.com", "bob-sub")["oidc_sub"] == "bob-sub"
        db.update_user(conn, "bob@example.com", active=False)
        assert db.reset_sign_in(conn, "bob@example.com")
    r = provider.sign_in(anonymous, email="bob@example.com", sub="evil", follow_redirects=False)
    assert r.status_code == 403 and "turned off" in r.text and "bob@example.com" in r.text
    assert auth.SESSION_COOKIE not in _set_cookies(r)
    assert "sign-in failed reason=inactive bob@example.com" in caplog.text
    assert _user(anonymous.app, "bob@example.com")["oidc_sub"] is None
    with closing(db.connect(url)) as conn:
        db.update_user(conn, "bob@example.com", active=True)
    r = provider.sign_in(anonymous, email="bob@example.com", sub="bob-sub", follow_redirects=False)
    assert r.status_code == 303 and auth.SESSION_COOKIE in _set_cookies(r)
    assert _user(anonymous.app, "bob@example.com")["oidc_sub"] == "bob-sub"


def test_a_reset_during_sign_in_ends_the_new_session(anonymous, provider, monkeypatch):
    link = db.link_account

    def link_then_reset(conn, id, sub, name):
        row = link(conn, id, sub, name)
        db.reset_sign_in(conn, id)
        return row

    monkeypatch.setattr(db, "link_account", link_then_reset)
    r = provider.sign_in(anonymous, follow_redirects=False)
    assert r.status_code == 303 and auth.SESSION_COOKIE in _set_cookies(r)
    assert anonymous.get("/", follow_redirects=False).status_code == 303


def test_sign_in_without_a_sub_fails(anonymous, provider, caplog):
    caplog.set_level("INFO", logger="verifair")
    _fails(provider.sign_in(anonymous, sub=None), "id-token", caplog)


def test_non_member_gets_no_session_and_signs_in_once_added(anonymous, shared_client, provider):
    r = provider.sign_in(anonymous, email="Eve@example.com", sub="eve-1", follow_redirects=False)
    assert r.status_code == 403 and "Ask an admin to add eve@example.com." in r.text
    assert auth.SESSION_COOKIE not in _set_cookies(r) and 'href="/login"' in r.text
    shared_client.post("/access/users", data={"id": "eve@example.com", "name": "Eve"})
    assert _user(anonymous.app, "eve@example.com")["oidc_sub"] is None
    provider.sign_in(anonymous, email="eve@example.com", sub="eve-2")
    assert anonymous.get("/", follow_redirects=False).status_code == 200
    assert _user(anonymous.app, "eve@example.com")["oidc_sub"] == "eve-2"


def test_sessions_issued_before_sessions_valid_after_are_refused(anonymous, caplog):
    caplog.set_level("INFO", logger="verifair")
    old = signed_in("admin@example.com")
    with closing(db.connect(anonymous.app.state.settings.database_url)) as conn:
        assert db.reset_sign_in(conn, "admin@example.com")
    r = anonymous.get("/r/x", headers=old, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/login?next=%2Fr%2Fx")
    assert "membership denied reason=signed-out path='/r/x'" in caplog.text
    assert anonymous.get("/", headers=signed_in("admin@example.com")).status_code == 200


def test_discovery_failure_is_cached_briefly(anonymous, provider, monkeypatch):
    calls = []

    def down(request):
        calls.append(request)
        raise httpx.ConnectError("refused")

    anonymous.app.state.oidc_transport = httpx.MockTransport(down)
    assert anonymous.get("/login", follow_redirects=False).status_code == 502
    assert anonymous.get("/login", follow_redirects=False).status_code == 502
    assert len(calls) == 1
    later = time.monotonic() + 31
    monkeypatch.setattr(auth.time, "monotonic", lambda: later)
    anonymous.app.state.oidc_transport = httpx.MockTransport(provider.handler)
    assert anonymous.get("/login", follow_redirects=False).status_code == 303


def test_invalid_provider_urls_fail_cleanly(shared_settings, make_app, provider, caplog):
    caplog.set_level("INFO", logger="verifair")
    app = make_app(replace(shared_settings, oidc_issuer="https://idp.example.com/\x7f"))
    assert TestClient(app).get("/login", follow_redirects=False).status_code == 502
    assert "sign-in failed reason=discovery InvalidURL" in caplog.text
    client = TestClient(make_app(shared_settings))
    client.app.state.oidc_transport = httpx.MockTransport(provider.handler)
    provider.discovery["token_endpoint"] = "https://idp.example.com/\x7f"
    assert provider.sign_in(client).status_code == 502
    assert "sign-in failed reason=token-endpoint InvalidURL" in caplog.text


UNREACHABLE = "We could not reach your sign-in provider. Try again in a moment."


def _token_response(anonymous, provider, respond):
    handler = provider.handler

    def transport(request):
        if request.url.path == "/token":
            return respond(request)
        return handler(request)

    anonymous.app.state.oidc_transport = httpx.MockTransport(transport)
    return provider.sign_in(anonymous)


def _timeout(request):
    raise httpx.ReadTimeout("slow")


@pytest.mark.parametrize(
    ("respond", "detail"),
    [
        (lambda r: httpx.Response(503, text="down"), "status 503"),
        (lambda r: httpx.Response(200, text="<html>"), "status 200 not JSON"),
        (_timeout, "ReadTimeout"),
    ],
)
def test_broken_token_endpoint_says_the_provider_is_unreachable(
    anonymous, provider, caplog, respond, detail
):
    caplog.set_level("INFO", logger="verifair")
    r = _token_response(anonymous, provider, respond)
    assert r.status_code == 502 and UNREACHABLE in r.text
    assert f"sign-in failed reason=token-endpoint {detail}" in caplog.text


def test_token_endpoint_error_value_is_logged(anonymous, provider, caplog):
    caplog.set_level("INFO", logger="verifair")
    error = {"error": "invalid_client", "error_description": "secret-ish detail"}
    r = _token_response(anonymous, provider, lambda r: httpx.Response(401, json=error))
    assert r.status_code == 400 and UNREACHABLE not in r.text
    assert "sign-in failed reason=token-endpoint status 401 error='invalid_client'" in caplog.text
    assert "secret-ish" not in caplog.text


def test_provider_redirect_error_value_is_logged(anonymous, provider, caplog):
    caplog.set_level("INFO", logger="verifair")
    params = provider.authorize(anonymous)
    anonymous.get(
        "/auth/callback",
        params={"state": params["state"], "error": "access_denied\nx", "error_description": "d"},
    )
    assert "sign-in failed reason=provider-error error='access_denied\\nx'" in caplog.text


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        (
            {"email_verified": False},
            "Your account has no verified email address that verifair can use."
            " Trying again will not help, so ask an admin.",
        ),
        (
            {"email": "x@other.org"},
            "Your email address is not from an allowed domain."
            " Trying again will not help, so ask an admin.",
        ),
        ({"nonce": "other"}, "Try again, or ask an admin if it keeps failing."),
    ],
)
def test_failure_page_wording_depends_on_the_reason(domain_client, provider, claims, message):
    r = provider.sign_in(domain_client(), **claims)
    assert message in r.text
    assert r.text.count("Sign-in failed") == 2


def test_second_tab_sign_in_keeps_the_newer_flow(anonymous, provider):
    first = provider.authorize(anonymous)
    second = provider.authorize(anonymous)
    r = anonymous.get("/auth/callback", params=first, follow_redirects=False)
    assert r.status_code == 400 and auth.FLOW_COOKIE not in _set_cookies(r)
    ok = anonymous.get("/auth/callback", params=second, follow_redirects=False)
    assert ok.status_code == 303


def test_failure_with_a_valid_session_returns_to_the_page(anonymous, provider):
    first = provider.authorize(anonymous, next="/changes")
    provider.claims = {}
    second = provider.authorize(anonymous, next="/r/x")
    assert anonymous.get("/auth/callback", params=second, follow_redirects=False).status_code == 303
    late = anonymous.get("/auth/callback", params=first, follow_redirects=False)
    assert (late.status_code, late.headers["location"]) == (303, "/")
    flow = provider.authorize(anonymous, next="/changes")
    r = anonymous.get("/auth/callback", params=flow | {"code": "unknown"}, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/changes")


def test_sign_in_link_on_the_401_page_keeps_the_page(anonymous):
    r = anonymous.get(
        "/r/x/partial",
        headers={"HX-Request": "true", "HX-Current-URL": "http://localhost:8000/r/x?tab=a"},
    )
    assert r.status_code == 401 and 'href="/login?next=%2Fr%2Fx%3Ftab%3Da"' in r.text
    post = anonymous.post(
        "/r/x/comments", data={"body": "x"}, headers={"Referer": "https://evil.com//x"}
    )
    assert post.status_code == 401 and 'href="/login?next=%2F%2Fx"' not in post.text
    for header in ("Referer", "HX-Current-URL"):
        bad = anonymous.post("/r/x/comments", data={"body": "x"}, headers={header: "http://[x/y"})
        assert bad.status_code == 401 and 'href="/login"' in bad.text
