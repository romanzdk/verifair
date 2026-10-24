import base64
import hashlib
import json
import os
import time
import uuid
from contextlib import closing
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import conninfo, sql

from verifair import auth, db
from verifair.app import create_app
from verifair.config import Settings

TEST_DATABASE_URL = os.environ.get(
    "VERIFAIR_TEST_DATABASE_URL", "postgresql://verifair:verifair@localhost:5432/verifair"
)
ISSUER = "https://idp.example.com"
CLIENT_ID = "verifair-client"
SESSION_SECRET = "test-session-secret-that-is-long-enough"
OIDC = {
    "oidc_issuer": ISSUER,
    "oidc_client_id": CLIENT_ID,
    "oidc_client_secret": "client-secret",
    "session_secret": SESSION_SECRET,
}


def signed_in(id: str, **cookies: str) -> dict:
    jar = {auth.SESSION_COOKIE: auth.make_session(SESSION_SECRET, id)} | cookies
    return {"Cookie": "; ".join(f"{k}={v}" for k, v in jar.items())}


class SessionClient(TestClient):
    # httpx rebuilds the Cookie header from the jar on a redirect, which would drop the
    # per-request session that tests send to act as different members
    def _redirect_headers(self, request, url, method):
        headers = super()._redirect_headers(request, url, method)
        if url.origin == request.url.origin and "Cookie" in request.headers:
            headers["Cookie"] = request.headers["Cookie"]
        return headers


def _b64(data: dict | bytes) -> str:
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class FakeProvider:
    def __init__(self):
        self.discovery = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
        }
        self.claims: dict = {}
        self.challenges: dict[str, tuple[str, str]] = {}
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.discovery)
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        challenge, nonce = self.challenges.get(form.get("code", ""), ("", ""))
        verifier = form.get("code_verifier", "").encode()
        if _b64(hashlib.sha256(verifier).digest()) != challenge:
            return httpx.Response(400, json={"error": "invalid_grant"})
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "123",
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
            "email": "admin@example.com",
            "email_verified": True,
        } | self.claims
        claims = {k: v for k, v in claims.items() if v is not None}
        id_token = f"{_b64({'alg': 'RS256'})}.{_b64(claims)}.{_b64(b'signature')}"
        return httpx.Response(200, json={"id_token": id_token, "access_token": "a"})

    def authorize(self, client, next: str = "/") -> dict:
        r = client.get("/login", params={"next": next}, follow_redirects=False)
        assert r.status_code == 303, r.text
        query = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
        code = f"code-{len(self.challenges)}"
        self.challenges[code] = (query["code_challenge"], query["nonce"])
        return {"code": code, "state": query["state"]}

    def sign_in(self, client, next: str = "/", follow_redirects: bool = True, **claims):
        self.claims = claims
        params = self.authorize(client, next)
        return client.get("/auth/callback", params=params, follow_redirects=follow_redirects)


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture(scope="session", autouse=True)
def admin_conn():
    try:
        c = psycopg.connect(TEST_DATABASE_URL, connect_timeout=3, autocommit=True)
    except psycopg.OperationalError:
        info = conninfo.conninfo_to_dict(TEST_DATABASE_URL)
        where = f"{info.get('host')}:{info.get('port', 5432)}/{info.get('dbname')}"
        pytest.exit(
            f"Postgres is not reachable at {where}. Run: docker compose up -d db", returncode=2
        )
    with c:
        yield c


@pytest.fixture(autouse=True)
def database_url_env(monkeypatch):
    monkeypatch.setenv("VERIFAIR_DATABASE_URL", "postgresql://unused")


@pytest.fixture
def new_database_url(admin_conn):
    schemas = []

    def create():
        schema = f"vt_{uuid.uuid4().hex[:12]}"
        admin_conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        schemas.append(schema)
        # tests need no crash durability, and not waiting for the WAL flush keeps the suite fast
        options = f"-c search_path={schema} -c synchronous_commit=off"
        return conninfo.make_conninfo(TEST_DATABASE_URL, options=options)

    yield create
    for schema in schemas:
        admin_conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def conn(new_database_url):
    c = db.connect(new_database_url())
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def make_app():
    apps = []

    def make(settings):
        app = create_app(settings)
        apps.append(app)
        return app

    yield make
    for app in apps:
        app.state.pool.close()


# a pool of one: a handler that borrows a second connection while holding one times out
@pytest.fixture
def settings(new_database_url):
    return Settings(
        api_token="t",
        database_url=new_database_url(),
        base_url="http://localhost:8000",
        slack_webhook_url=None,
        required_sections=(),
        db_pool_size=1,
    )


@pytest.fixture
def client(settings, make_app):
    return TestClient(make_app(settings), headers={"Sec-Fetch-Site": "same-origin"})


@pytest.fixture
def shared_settings(new_database_url):
    return Settings(
        api_token=None,
        database_url=new_database_url(),
        base_url="http://localhost:8000",
        slack_webhook_url=None,
        bootstrap_admin_id="admin@example.com",
        db_pool_size=1,
        **OIDC,
    )


@pytest.fixture
def shared_client(shared_settings, make_app, provider):
    app = make_app(shared_settings)
    app.state.oidc_transport = httpx.MockTransport(provider.handler)
    return SessionClient(
        app, headers={"Sec-Fetch-Site": "same-origin"} | signed_in("admin@example.com")
    )


@pytest.fixture
def as_user(shared_client, shared_settings):
    def member(id, name=None):
        with closing(db.connect(shared_settings.database_url)) as conn:
            db.add_user(conn, id=id, name=name or id.partition("@")[0], role="member")
        return signed_in(id)

    return member
