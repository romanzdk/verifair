import base64
import hashlib
import hmac
import json
import logging
import math
import secrets
import time
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import RedirectResponse

from verifair import web
from verifair.config import Settings, https_or_local

SESSION_COOKIE = "vf_session"
FLOW_COOKIE = "vf_signin"
SESSION_AGE = 7 * 24 * 3600
FLOW_AGE = 600
SKEW = 120
NEXT_LIMIT = 2000
GOOGLE = "https://accounts.google.com"
FAILED = "Sign-in failed. Try again, or ask an admin if it keeps failing."
log = logging.getLogger("verifair")
router = APIRouter()


class SignInError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret: str, purpose: str, body: str) -> str:
    # the purpose is signed too, so a sign-in flow cookie never passes as a session
    message = f"{purpose}:{body}".encode()
    return _b64(hmac.new(secret.encode(), message, hashlib.sha256).digest())


def sign(secret: str, purpose: str, payload) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_mac(secret, purpose, body)}"


def unsign(secret: str, purpose: str, value: str) -> dict | None:
    body, _, mac = value.rpartition(".")
    if not hmac.compare_digest(_mac(secret, purpose, body).encode(), mac.encode()):
        return None
    try:
        payload = json.loads(_unb64(body))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _instant(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def make_session(secret: str, member_id: str, now: float | None = None) -> str:
    issued = int(time.time() if now is None else now)
    return sign(secret, "session", {"sub": member_id, "iat": issued, "exp": issued + SESSION_AGE})


def read_session(secret: str, value: str, now: float | None = None) -> str | None:
    now = time.time() if now is None else now
    data = unsign(secret, "session", value)
    if data is None:
        return None
    sub, iat, exp = data.get("sub"), data.get("iat"), data.get("exp")
    if not (isinstance(sub, str) and sub and _instant(iat) and _instant(exp)):
        return None
    return sub if iat <= now + SKEW and now < exp else None


def safe_next(value: str) -> str:
    # isprintable refuses tabs and newlines, which browsers drop, so "/\t/x" cannot become "//x"
    if (
        value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and len(value) <= NEXT_LIMIT
        and value.isprintable()
    ):
        return value
    return "/"


def _same_issuer(value, issuer: str) -> bool:
    return isinstance(value, str) and value.removesuffix("/") == issuer.removesuffix("/")


def check_id_token(id_token: str, settings: Settings, nonce: str, now: float) -> str:
    parts = id_token.split(".")
    try:
        claims = json.loads(_unb64(parts[1])) if len(parts) == 3 else None
    except ValueError:
        claims = None
    if not isinstance(claims, dict):
        raise SignInError("id-token")
    if not _same_issuer(claims.get("iss"), settings.oidc_issuer):
        raise SignInError("iss")
    client_id, aud, azp = settings.oidc_client_id, claims.get("aud"), claims.get("azp")
    audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    if client_id not in audiences or ((azp is not None or len(audiences) > 1) and azp != client_id):
        raise SignInError("aud")
    exp, iat = claims.get("exp"), claims.get("iat")
    if not (_instant(exp) and now < exp + SKEW):
        raise SignInError("exp")
    if not (_instant(iat) and iat <= now + SKEW):
        raise SignInError("iat")
    got = claims.get("nonce")
    if not (isinstance(got, str) and hmac.compare_digest(got.encode(), nonce.encode())):
        raise SignInError("nonce")
    email = claims.get("email")
    if not (
        isinstance(email, str)
        and claims.get("email_verified") is True
        and "@" in email
        and web.plain_id(email)
    ):
        raise SignInError("email")
    member_id = email.lower()
    domains = settings.oidc_allowed_domains
    if domains:
        hd = claims.get("hd")
        # Google sets hd only for Workspace accounts, so a missing hd there is a personal account
        hd_needed = hd is not None or settings.oidc_issuer.removesuffix("/") == GOOGLE
        if member_id.rpartition("@")[2] not in domains or (
            hd_needed and not (isinstance(hd, str) and hd.lower() in domains)
        ):
            raise SignInError("domain")
    return member_id


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=app.state.oidc_transport, timeout=10)


async def _discover(app: FastAPI) -> dict:
    if app.state.oidc_config is None:
        issuer = app.state.settings.oidc_issuer
        try:
            async with _client(app) as client:
                response = await client.get(
                    f"{issuer.removesuffix('/')}/.well-known/openid-configuration"
                )
            response.raise_for_status()
            doc = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise SignInError("discovery", type(error).__name__) from error
        endpoints = ("authorization_endpoint", "token_endpoint")
        if not (
            isinstance(doc, dict)
            and _same_issuer(doc.get("issuer"), issuer)
            and all(https_or_local(doc.get(key)) for key in endpoints)
        ):
            raise SignInError("discovery", "issuer or endpoints do not match")
        app.state.oidc_config = doc
    return app.state.oidc_config


def _redirect_uri(settings: Settings) -> str:
    return f"{settings.base_url}/auth/callback"


async def _exchange(app: FastAPI, config: dict, code: str, verifier: str) -> str:
    settings = app.state.settings
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _redirect_uri(settings),
        "code_verifier": verifier,
        "client_id": settings.oidc_client_id,
    }
    auth = None
    if "client_secret_post" in (config.get("token_endpoint_auth_methods_supported") or ()):
        data["client_secret"] = settings.oidc_client_secret
    else:
        # RFC 6749 2.3.1: Basic credentials are form-encoded before base64
        id, secret = settings.oidc_client_id, settings.oidc_client_secret
        auth = httpx.BasicAuth(quote(id, safe=""), quote(secret, safe=""))
    try:
        async with _client(app) as client:
            response = await client.post(
                config["token_endpoint"],
                data=data,
                auth=auth,
                headers={"Accept": "application/json"},
            )
        body = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError) as error:
        raise SignInError("token-endpoint", type(error).__name__) from error
    if not (isinstance(body, dict) and isinstance(body.get("id_token"), str)):
        raise SignInError("token-endpoint", f"status {response.status_code}")
    return body["id_token"]


def _set_cookie(request: Request, response, name: str, value: str, max_age: int):
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.app.state.settings.base_url.startswith("https"),
    )


def _clear_cookie(request: Request, response, name: str):
    secure = request.app.state.settings.base_url.startswith("https")
    response.delete_cookie(name, path="/", httponly=True, samesite="lax", secure=secure)


def page(request: Request, title: str, message: str, status: int, **ctx):
    ctx = {"title": title, "message": message, "home": False} | ctx
    return web.templates.TemplateResponse(request, "denied.html", ctx, status_code=status)


def _failed(request: Request, error: SignInError, status: int):
    log.info("sign-in failed reason=%s", " ".join(map(str, error.args)))
    return page(request, "Sign-in failed", FAILED, status, signin=True)


@router.get("/login")
async def login(request: Request, next: str = "/"):
    settings = request.app.state.settings
    try:
        config = await _discover(request.app)
    except SignInError as error:
        return _failed(request, error, 502)
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.oidc_client_id,
            "redirect_uri": _redirect_uri(settings),
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
        }
    )
    endpoint = config["authorization_endpoint"]
    response = RedirectResponse(f"{endpoint}{'&' if '?' in endpoint else '?'}{query}", 303)
    flow = {
        "state": state,
        "nonce": nonce,
        "verifier": verifier,
        "next": safe_next(next),
        "exp": int(time.time()) + FLOW_AGE,
    }
    _set_cookie(
        request, response, FLOW_COOKIE, sign(settings.session_secret, "flow", flow), FLOW_AGE
    )
    return response


@router.get("/auth/callback")
async def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    settings = request.app.state.settings
    flow = unsign(settings.session_secret, "flow", request.cookies.get(FLOW_COOKIE, ""))
    try:
        if flow is None or not (_instant(flow.get("exp")) and time.time() < flow["exp"]):
            raise SignInError("no-flow")
        if not hmac.compare_digest(state.encode(), str(flow.get("state")).encode()):
            raise SignInError("state")
        if error:
            raise SignInError("provider-error")
        if not code:
            raise SignInError("no-code")
        id_token = await _exchange(
            request.app, await _discover(request.app), code, flow["verifier"]
        )
        member_id = check_id_token(id_token, settings, flow["nonce"], time.time())
    except SignInError as failure:
        response = _failed(request, failure, 400)
        _clear_cookie(request, response, FLOW_COOKIE)
        return response
    log.info("signed in id=%s", member_id)
    response = RedirectResponse(flow["next"], 303)
    session = make_session(settings.session_secret, member_id)
    _set_cookie(request, response, SESSION_COOKIE, session, SESSION_AGE)
    _clear_cookie(request, response, FLOW_COOKIE)
    return response


@router.post("/logout")
def logout(request: Request):
    response = RedirectResponse("/signed-out", 303)
    _clear_cookie(request, response, SESSION_COOKIE)
    return response


@router.get("/signed-out")
def signed_out(request: Request):
    return page(request, "Signed out", "You signed out.", 200, signin=True)
