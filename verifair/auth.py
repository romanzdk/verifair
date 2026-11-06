import base64
import hashlib
import hmac
import json
import logging
import math
import secrets
import time
import unicodedata
from urllib.parse import quote, urlencode

import httpx
import psycopg
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import RedirectResponse

from verifair import db, web
from verifair.config import GOOGLE, Settings, https_or_local
from verifair.deps import run_with_conn

SESSION_COOKIE = "vf_session"
FLOW_COOKIE = "vf_signin"
SESSION_AGE = 7 * 24 * 3600
FLOW_AGE = 600
SKEW = 120
NEXT_LIMIT = 2000
DISCOVERY_RETRY = 30
FAILED = "Try again, or ask an admin if it keeps failing."
UNREACHABLE = "We could not reach your sign-in provider. Try again in a moment."
REFUSED = {
    "email": "Your account has no verified email address that verifair can use."
    " Trying again will not help, so ask an admin.",
    "domain": "Your email address is not from an allowed domain."
    " Trying again will not help, so ask an admin.",
    "account": "This email is linked to a different account. Ask an admin to reset your sign-in.",
}
log = logging.getLogger("verifair")
router = APIRouter()


class SignInError(Exception):
    pass


class Unreachable(SignInError):
    pass


def cookie_name(settings: Settings, name: str) -> str:
    # __Host- makes browsers refuse the cookie unless it is Secure, for Path=/ and without a
    # Domain, so a sibling subdomain or a plain-http page cannot plant or overwrite it
    return f"__Host-{name}" if settings.https else name


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
    # a fractional iat, so a session issued just after a reset in the same second still counts
    issued = time.time() if now is None else now
    return sign(secret, "session", {"sub": member_id, "iat": issued, "exp": issued + SESSION_AGE})


def read_session(secret: str, value: str, now: float | None = None) -> tuple[str, float] | None:
    now = time.time() if now is None else now
    data = unsign(secret, "session", value)
    if data is None:
        return None
    sub, iat, exp = data.get("sub"), data.get("iat"), data.get("exp")
    if not (isinstance(sub, str) and sub and _instant(iat) and _instant(exp)):
        return None
    return (sub, iat) if iat <= now + SKEW and now < exp else None


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


def _token_issuer(value, issuer: str) -> bool:
    # Google may put its issuer in ID tokens without the scheme
    google = issuer.removesuffix("/") == GOOGLE and value == "accounts.google.com"
    return google or _same_issuer(value, issuer)


# letters that render as nothing, so "ㅤ" would pass as a name and show as blank
BLANK_LOOKING = dict.fromkeys(map(ord, "\u115f\u1160\u3164\uffa0\u2800"), " ")


def check_id_token(
    id_token: str, settings: Settings, nonce: str, now: float
) -> tuple[str, str, str | None]:
    parts = id_token.split(".")
    try:
        claims = json.loads(_unb64(parts[1])) if len(parts) == 3 else None
    except ValueError:
        claims = None
    if not isinstance(claims, dict):
        raise SignInError("id-token")
    if not _token_issuer(claims.get("iss"), settings.oidc_issuer):
        raise SignInError("iss")
    sub = claims.get("sub")
    if not (isinstance(sub, str) and sub):
        raise SignInError("id-token", "no sub")
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
        isinstance(email, str) and claims.get("email_verified") is True and web.email_id(email)
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
    name = claims.get("name")
    named = settings.open_signup or member_id == (settings.bootstrap_admin_id or "").lower()
    if not isinstance(name, str) or not named:
        return member_id, sub, None
    # control, bidi and zero-width characters could make the name look like someone else's
    name = "".join(c for c in name if unicodedata.category(c) not in ("Cc", "Cf"))
    name = " ".join(name.translate(BLANK_LOOKING).split())[:60].rstrip()
    readable = any(c.isalnum() for c in name)
    return member_id, sub, name if readable and db.storable(name) else None


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=app.state.oidc_transport, timeout=10)


async def _discover(app: FastAPI) -> dict:
    if app.state.oidc_config is None:
        failure = app.state.oidc_failure
        # a provider that is down would otherwise make every sign-in wait for the timeout
        if failure is not None and time.monotonic() < failure[0]:
            raise Unreachable("discovery", failure[1], "(cached)")
        try:
            app.state.oidc_config = await _fetch_config(app)
        except SignInError as error:
            app.state.oidc_failure = (time.monotonic() + DISCOVERY_RETRY, error.args[1])
            raise
    return app.state.oidc_config


async def _fetch_config(app: FastAPI) -> dict:
    issuer = app.state.settings.oidc_issuer
    try:
        async with _client(app) as client:
            response = await client.get(
                f"{issuer.removesuffix('/')}/.well-known/openid-configuration"
            )
        response.raise_for_status()
        doc = response.json()
    except (httpx.HTTPError, httpx.InvalidURL, ValueError) as error:
        raise Unreachable("discovery", type(error).__name__) from error
    endpoints = ("authorization_endpoint", "token_endpoint")
    if not (
        isinstance(doc, dict)
        and _same_issuer(doc.get("issuer"), issuer)
        and all(https_or_local(doc.get(key)) for key in endpoints)
    ):
        raise Unreachable("discovery", "issuer or endpoints do not match")
    return doc


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
    except (httpx.HTTPError, httpx.InvalidURL) as error:
        raise Unreachable("token-endpoint", type(error).__name__) from error
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        body = None
    if status >= 500 or (status == 200 and body is None):
        raise Unreachable(
            "token-endpoint", f"status {status}" + (" not JSON" if body is None else "")
        )
    if status != 200 or not (isinstance(body, dict) and isinstance(body.get("id_token"), str)):
        error = body.get("error") if isinstance(body, dict) else None
        detail = f" error={str(error)[:100]!r}" if error is not None else ""
        raise SignInError("token-endpoint", f"status {status}{detail}")
    return body["id_token"]


def _set_cookie(request: Request, response, name: str, value: str, max_age: int):
    response.set_cookie(
        cookie_name(request.app.state.settings, name),
        value,
        max_age=max_age,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.app.state.settings.https,
    )


def clear_cookie(request: Request, response, name: str):
    settings = request.app.state.settings
    response.delete_cookie(
        cookie_name(settings, name),
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.https,
    )


def _cookie(request: Request, name: str) -> str:
    return request.cookies.get(cookie_name(request.app.state.settings, name), "")


def page(request: Request, title: str, message: str, status: int, **ctx):
    ctx = {"title": title, "message": message, "home": False} | ctx
    return web.templates.TemplateResponse(request, "denied.html", ctx, status_code=status)


def _failed(request: Request, error: SignInError):
    log.info("sign-in failed reason=%s", " ".join(map(str, error.args)))
    other = {"signin": True, "signin_label": "Sign in with another account"}
    retry = {"signin": True, "signin_label": "Try again"}
    if isinstance(error, Unreachable):
        return page(request, "Sign-in failed", UNREACHABLE, 502, **retry)
    if error.args[0] in ("not-member", "inactive"):
        copy = web.NOT_MEMBER if error.args[0] == "not-member" else web.DEACTIVATED
        return page(request, "No access", copy.format(id=error.args[1]), 403, **other)
    if error.args[0] in REFUSED:
        return page(request, "Sign-in failed", REFUSED[error.args[0]], 400, **other)
    return page(request, "Sign-in failed", FAILED, 400, **retry)


@router.get("/login")
async def login(request: Request, next: str = "/"):
    settings = request.app.state.settings
    try:
        config = await _discover(request.app)
    except SignInError as error:
        return _failed(request, error)
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
    flow = unsign(settings.session_secret, "flow", _cookie(request, FLOW_COOKIE))
    if flow is not None and not (_instant(flow.get("exp")) and time.time() < flow["exp"]):
        flow = None
    try:
        if flow is None:
            raise SignInError("no-flow")
        if not hmac.compare_digest(state.encode(), str(flow.get("state")).encode()):
            raise SignInError("state")
        if error:
            raise SignInError("provider-error", f"error={error[:100]!r}")
        if not code:
            raise SignInError("no-code")
        id_token = await _exchange(
            request.app, await _discover(request.app), code, flow["verifier"]
        )
        # taken before linking, so a reset that lands meanwhile also ends this session
        now = time.time()
        member_id, sub, name = check_id_token(id_token, settings, flow["nonce"], now)
        # no session for someone who is not a member yet: it would carry no linked account
        bootstrap = member_id == (settings.bootstrap_admin_id or "").lower()
        # only the bootstrap admin and people joining get the provider's name; a name an
        # admin typed when adding someone stays
        linked = await run_with_conn(
            request.app, db.link_account, member_id, sub, name if bootstrap else None
        )
        if linked is None and settings.open_signup:
            if await run_with_conn(request.app, db.join, member_id, name):
                log.info("access action=join target=%s", member_id)
            linked = await run_with_conn(request.app, db.link_account, member_id, sub)
        if linked is None:
            raise SignInError("not-member", member_id)
        if not linked["active"]:
            raise SignInError("inactive", member_id)
        if linked["oidc_sub"] != sub:
            raise SignInError("account")
    except SignInError as failure:
        # another tab may have signed in already, or still be signing in with the newer flow
        next = safe_next(str(flow.get("next"))) if flow else "/"
        if read_session(settings.session_secret, _cookie(request, SESSION_COOKIE)):
            log.info("sign-in failed reason=%s, kept the session", " ".join(map(str, failure.args)))
            response = RedirectResponse(next, 303)
        else:
            response = _failed(request, failure)
        if failure.args[0] != "state":
            clear_cookie(request, response, FLOW_COOKIE)
        return response
    log.info("signed in id=%s", member_id)
    response = RedirectResponse(flow["next"], 303)
    session = make_session(settings.session_secret, member_id, now=now)
    _set_cookie(request, response, SESSION_COOKIE, session, SESSION_AGE)
    clear_cookie(request, response, FLOW_COOKIE)
    return response


@router.post("/logout")
async def logout(request: Request):
    settings = request.app.state.settings
    # the cookie is signed, not stored, so only this ends a copy of it kept elsewhere
    session = read_session(settings.session_secret, _cookie(request, SESSION_COOKIE))
    if session:
        try:
            await run_with_conn(request.app, db.end_sessions, session[0])
        except psycopg.Error as error:
            # still sign this browser out; only copies of the cookie elsewhere stay valid
            log.error("sign-out could not end other sessions id=%s: %s", session[0], error)
    response = RedirectResponse("/signed-out", 303)
    clear_cookie(request, response, SESSION_COOKIE)
    return response


@router.get("/signed-out")
def signed_out(request: Request):
    return page(request, "Signed out", "You signed out.", 200, signin=True)
