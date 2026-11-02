"""
A small, real OpenID Connect provider for testing Slate's OAuth login.
Not part of the product — test tooling, used by verify_oauth.py, the
same way the SAML and WebAuthn verification used hand-built test IdPs
and authenticators rather than mocks.

What makes it a meaningful test of Slate rather than a mirror of it:

  * It signs with **joserfc** (RS256, real RSA keys). Slate's client
    verifies with **python-jose**. Two unrelated JWT implementations
    agreeing on a token is the point; a provider built from the same
    library would only prove the library agrees with itself.
  * It enforces what real providers enforce: exact redirect-URI
    matching, client authentication (HTTP Basic or form body), PKCE
    S256, and single-use, short-lived authorization codes.
  * The interactive step is real too: GET /authorize renders a sign-in
    form and POST /authorize authenticates and redirects with a code.

Test-only controls live under /_test/ — registering redirect URIs and
users, rotating the signing key, and arming the *next* ID token with a
specific defect (tampered signature, wrong audience, alg=none, ...) so
the client's rejection paths get exercised over real HTTP, not just
unit-tested.

    TEST_OIDC_PORT=9100 python backend/scripts/oidc_test_provider.py

Environment: TEST_OIDC_PORT (9100), TEST_OIDC_ISSUER (http://127.0.0.1:PORT),
TEST_OIDC_CLIENT_ID, TEST_OIDC_CLIENT_SECRET, TEST_OIDC_PASSWORD,
TEST_OIDC_AUTH_METHOD = both | basic | post (which client-authentication
methods /token accepts).
"""

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from typing import Optional
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from joserfc import jwt
from joserfc.jwk import RSAKey
from pydantic import BaseModel

PORT = int(os.getenv("TEST_OIDC_PORT", "9100"))
ISSUER = os.getenv("TEST_OIDC_ISSUER", f"http://127.0.0.1:{PORT}")
CLIENT_ID = os.getenv("TEST_OIDC_CLIENT_ID", "slate-test-client")
CLIENT_SECRET = os.getenv("TEST_OIDC_CLIENT_SECRET", "test-client-secret-1234567890")
USER_PASSWORD = os.getenv("TEST_OIDC_PASSWORD", "correct-horse-battery")
AUTH_METHOD = os.getenv("TEST_OIDC_AUTH_METHOD", "both")  # both | basic | post
CODE_TTL_SECONDS = 60

app = FastAPI(title="Slate test OIDC provider")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def new_key(kid: Optional[str] = None) -> RSAKey:
    kid = kid or f"test-key-{secrets.token_hex(4)}"
    return RSAKey.generate_key(2048, parameters={"kid": kid, "use": "sig", "alg": "RS256"})


class State:
    """All in memory — a test provider's state should vanish with it."""

    def __init__(self):
        self.key = new_key()
        self.foreign_key = new_key()  # never published; used to forge tokens
        self.retired_key = None  # the previous signing key, after a rotation — no longer in the JWKS
        self.redirect_uris: set = set()
        self.users: dict = {}
        self.codes: dict = {}
        self.armed: dict = {"mode": None, "count": 0}
        self.jwks_failures = 0
        self.log: list = []


S = State()


# --------------------------------------------------------------------------
# Discovery + keys
# --------------------------------------------------------------------------

@app.get("/.well-known/openid-configuration")
def discovery():
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "email", "profile"],
        "token_endpoint_auth_methods_supported": {
            "both": ["client_secret_basic", "client_secret_post"], "basic": ["client_secret_basic"], "post": ["client_secret_post"],
        }[AUTH_METHOD],
        "code_challenge_methods_supported": ["S256"],
    }


@app.get("/.well-known/jwks.json")
def jwks():
    if S.jwks_failures > 0:
        S.jwks_failures -= 1
        return PlainTextResponse("jwks temporarily broken", status_code=500)
    return {"keys": [S.key.as_dict(private=False)]}


# --------------------------------------------------------------------------
# Authorization endpoint: a real sign-in form, real validation
# --------------------------------------------------------------------------

AUTHORIZE_FIELDS = ["response_type", "client_id", "redirect_uri", "scope", "state", "nonce", "code_challenge", "code_challenge_method"]


def validate_authorization_request(p: dict):
    """Returns (error_page_message, redirect_error). An unknown client or
    an unregistered redirect_uri is shown to the person as a page and
    NEVER redirected to — redirecting there would make the provider an
    open redirector. Everything else goes back to the client as ?error=."""
    if p.get("client_id") != CLIENT_ID:
        return "Unknown client_id", None
    if p.get("redirect_uri") not in S.redirect_uris:
        return "redirect_uri is not registered for this client", None
    if p.get("response_type") != "code":
        return None, "unsupported_response_type"
    if "openid" not in (p.get("scope") or "").split():
        return None, "invalid_scope"
    if not p.get("state"):
        return None, "invalid_request"
    if not p.get("code_challenge") or p.get("code_challenge_method") != "S256":
        return None, "invalid_request"  # this provider requires PKCE
    return None, None


def redirect_with(redirect_uri: str, **params) -> RedirectResponse:
    parts = urlsplit(redirect_uri)
    query = parts.query + ("&" if parts.query else "") + urlencode({k: v for k, v in params.items() if v is not None})
    return RedirectResponse(urlunsplit((parts.scheme, parts.netloc, parts.path, query, "")), status_code=302)


def login_form(p: dict, error: str = "") -> str:
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(p.get(k) or "")}">' for k in AUTHORIZE_FIELDS
    )
    return f"""<!doctype html><html><body>
<h2>Test provider sign-in</h2>
<p>Slate is asking to read your email and profile.</p>
{f'<p style="color:#b00">{html.escape(error)}</p>' if error else ''}
<form method="post" action="/authorize">{hidden}
<input name="username" placeholder="email"> <input name="password" type="password" placeholder="password">
<button name="action" value="approve">Sign in</button> <button name="action" value="deny">Deny</button>
</form></body></html>"""


@app.get("/authorize")
async def authorize_get(request: Request):
    p = dict(request.query_params)
    page_error, redirect_error = validate_authorization_request(p)
    if page_error:
        return HTMLResponse(f"<h2>Error</h2><p>{html.escape(page_error)}</p>", status_code=400)
    if redirect_error:
        return redirect_with(p["redirect_uri"], error=redirect_error, state=p.get("state"))
    return HTMLResponse(login_form(p))


@app.post("/authorize")
async def authorize_post(request: Request):
    form = await request.form()
    p = {k: form.get(k) for k in AUTHORIZE_FIELDS}
    page_error, redirect_error = validate_authorization_request(p)  # hidden fields are not trusted
    if page_error:
        return HTMLResponse(f"<h2>Error</h2><p>{html.escape(page_error)}</p>", status_code=400)
    if redirect_error:
        return redirect_with(p["redirect_uri"], error=redirect_error, state=p.get("state"))
    if form.get("action") == "deny":
        return redirect_with(p["redirect_uri"], error="access_denied", state=p["state"])

    user = S.users.get((form.get("username") or "").strip().lower())
    if user is None or not hmac.compare_digest((form.get("password") or "").encode(), USER_PASSWORD.encode()):
        return HTMLResponse(login_form(p, "Incorrect email or password"), status_code=401)

    code = secrets.token_urlsafe(32)
    S.codes[code] = {
        "client_id": CLIENT_ID, "redirect_uri": p["redirect_uri"], "user": user, "nonce": p.get("nonce"),
        "code_challenge": p["code_challenge"], "scope": p["scope"], "created": time.time(), "used": False,
    }
    return redirect_with(p["redirect_uri"], code=code, state=p["state"])


# --------------------------------------------------------------------------
# Token endpoint
# --------------------------------------------------------------------------

def token_error(status: int, error: str, **log_extra):
    S.log.append({"endpoint": "/token", "ok": False, "status": status, "error": error, **log_extra})
    headers = {"WWW-Authenticate": 'Basic realm="token"'} if status == 401 else {}
    return JSONResponse({"error": error}, status_code=status, headers={"Cache-Control": "no-store", **headers})


def authenticate_client(request: Request, form):
    """Returns (ok, method_used). Enforces AUTH_METHOD and RFC 6749
    §2.3: a request must use exactly one client-authentication method."""
    header = request.headers.get("authorization", "")
    has_basic = header.lower().startswith("basic ")
    has_post = form.get("client_secret") is not None
    if has_basic and has_post:
        return False, "both"
    if has_basic:
        if AUTH_METHOD == "post":
            return False, "basic"
        try:
            decoded = base64.b64decode(header[6:]).decode()
            raw_id, _, raw_secret = decoded.partition(":")
            client_id, client_secret = unquote(raw_id.replace("+", "%20")), unquote(raw_secret.replace("+", "%20"))
        except Exception:
            return False, "basic"
        ok = client_id == CLIENT_ID and hmac.compare_digest(client_secret.encode(), CLIENT_SECRET.encode())
        return ok, "basic"
    if has_post:
        if AUTH_METHOD == "basic":
            return False, "post"
        ok = form.get("client_id") == CLIENT_ID and hmac.compare_digest(form.get("client_secret", "").encode(), CLIENT_SECRET.encode())
        return ok, "post"
    return False, "none"


def take_armed_mode() -> Optional[str]:
    if S.armed["count"] > 0:
        S.armed["count"] -= 1
        return S.armed["mode"]
    return None


def build_id_token(rec: dict, access_token: str, mode: Optional[str]) -> str:
    now = int(time.time())
    user = rec["user"]
    claims = {
        "iss": ISSUER, "sub": user["sub"], "aud": CLIENT_ID, "exp": now + 300, "iat": now, "auth_time": now,
        "email": user["email"], "name": user["name"], "nonce": rec["nonce"],
        # Like Google's: present, and not something Slate uses (it never touches the access token)
        "at_hash": b64url(hashlib.sha256(access_token.encode()).digest()[:16]),
    }
    if user.get("email_verified") is not None:
        claims["email_verified"] = user["email_verified"]
    header = {"alg": "RS256", "kid": S.key.kid, "typ": "JWT"}
    key = S.key

    if mode == "wrong_audience":
        claims["aud"] = "some-other-client"
    elif mode == "multi_aud":
        claims["aud"] = [CLIENT_ID, "some-other-client"]  # no azp
    elif mode == "wrong_issuer":
        claims["iss"] = "https://evil.example.com"
    elif mode == "expired":
        claims["exp"], claims["iat"] = now - 3600, now - 7200
    elif mode == "missing_exp":
        del claims["exp"]
    elif mode == "wrong_nonce":
        claims["nonce"] = "attacker-chosen-nonce"
    elif mode == "no_nonce":
        del claims["nonce"]
    elif mode == "no_email":
        del claims["email"]
    elif mode == "unverified_email":
        claims["email_verified"] = False
    elif mode == "foreign_key":
        key = S.foreign_key  # attacker's own key...
        header["kid"] = S.key.kid  # ...claiming to be the provider's
    elif mode == "unknown_kid":
        header["kid"] = "kid-nobody-published"
    elif mode == "retired_key":
        key = S.retired_key or S.foreign_key  # genuinely signed by a real key the provider has since withdrawn
        header["kid"] = key.kid

    if mode == "alg_none":
        # Unsecured JWT: header says alg=none, signature empty
        return f"{b64url(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())}.{b64url(json.dumps(claims).encode())}."
    if mode == "hs256_confusion":
        # The classic algorithm-confusion forgery: HMAC-SHA256 keyed with the provider's *public* key
        h = b64url(json.dumps({"alg": "HS256", "kid": S.key.kid, "typ": "JWT"}).encode())
        pl = b64url(json.dumps(claims).encode())
        sig = hmac.new(S.key.as_pem(private=False), f"{h}.{pl}".encode(), hashlib.sha256).digest()
        return f"{h}.{pl}.{b64url(sig)}"

    token = jwt.encode(header, claims, key)

    if mode == "tamper_signature":
        h, pl, sig = token.split(".")
        raw = bytearray(b64url_decode(sig))
        raw[0] ^= 0x01
        return f"{h}.{pl}.{b64url(bytes(raw))}"
    if mode == "tamper_payload":
        # Mutated *after* signing: swap in a different email, keep the original signature
        h, pl, sig = token.split(".")
        payload = json.loads(b64url_decode(pl))
        payload["email"] = "attacker@evil.example"
        return f"{h}.{b64url(json.dumps(payload).encode())}.{sig}"
    return token


@app.post("/token")
async def token(request: Request):
    form = await request.form()
    ok, method = authenticate_client(request, form)
    if not ok:
        return token_error(401 if method != "both" else 400, "invalid_client" if method != "both" else "invalid_request", auth_method=method)
    if form.get("grant_type") != "authorization_code":
        return token_error(400, "unsupported_grant_type", auth_method=method)

    armed = take_armed_mode()
    if armed == "token_500":
        S.log.append({"endpoint": "/token", "ok": False, "status": 500, "error": "armed token_500", "auth_method": method})
        return PlainTextResponse("upstream exploded", status_code=500)

    code = form.get("code") or ""
    rec = S.codes.get(code)
    if rec is None:
        return token_error(400, "invalid_grant", auth_method=method, reason="unknown code")
    if rec["used"]:
        rec["reuse_detected"] = True  # RFC 6749 §4.1.2: treat reuse as an attack (a real provider would revoke what it issued)
        return token_error(400, "invalid_grant", auth_method=method, reason="code reused")
    rec["used"] = True  # single-use from the first redemption attempt that authenticates, success or not
    if time.time() - rec["created"] > CODE_TTL_SECONDS:
        return token_error(400, "invalid_grant", auth_method=method, reason="code expired")
    if rec["client_id"] != CLIENT_ID or form.get("redirect_uri") != rec["redirect_uri"]:
        return token_error(400, "invalid_grant", auth_method=method, reason="redirect_uri mismatch")
    verifier = form.get("code_verifier") or ""
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    if not verifier or not hmac.compare_digest(challenge.encode(), rec["code_challenge"].encode()):
        return token_error(400, "invalid_grant", auth_method=method, reason="PKCE verification failed")

    access_token = secrets.token_urlsafe(32)
    id_token = build_id_token(rec, access_token, armed)
    S.log.append({"endpoint": "/token", "ok": True, "status": 200, "auth_method": method, "pkce": True, "mode": armed, "kid": S.key.kid})
    return JSONResponse(
        {"access_token": access_token, "token_type": "Bearer", "expires_in": 3600, "scope": rec["scope"], "id_token": id_token},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# --------------------------------------------------------------------------
# Test-only controls
# --------------------------------------------------------------------------

class RedirectIn(BaseModel):
    uri: str


class UserIn(BaseModel):
    email: str
    name: str
    sub: Optional[str] = None
    email_verified: Optional[bool] = True  # None = omit the claim entirely, like Azure AD


class ArmIn(BaseModel):
    mode: str
    count: int = 1


@app.post("/_test/redirect-uri")
def register_redirect(body: RedirectIn):
    S.redirect_uris.add(body.uri)
    return {"registered": sorted(S.redirect_uris)}


@app.post("/_test/users")
def add_user(body: UserIn):
    S.users[body.email.strip().lower()] = {
        "email": body.email, "name": body.name, "sub": body.sub or f"sub-{secrets.token_hex(6)}",
        "email_verified": body.email_verified,
    }
    return {"ok": True}


@app.post("/_test/arm")
def arm(body: ArmIn):
    """Arms the next `count` ID-token issuances with a defect. 'jwks_500'
    instead breaks the next `count` JWKS fetches."""
    if body.mode == "jwks_500":
        S.jwks_failures = body.count
    else:
        S.armed = {"mode": body.mode, "count": body.count}
    return {"armed": body.mode, "count": body.count}


@app.post("/_test/rotate-key")
def rotate_key():
    old = S.key.kid
    S.retired_key = S.key
    S.key = new_key()
    return {"old_kid": old, "new_kid": S.key.kid}


@app.get("/_test/log")
def get_log():
    return {"log": S.log, "kid": S.key.kid, "reused_codes": [c[:6] for c, r in S.codes.items() if r.get("reuse_detected")]}


@app.get("/_test/health")
def health():
    return {"status": "ok", "issuer": ISSUER}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
