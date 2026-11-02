"""
Real OAuth 2.0 / OpenID Connect sign-in — the authorization-code flow,
with Slate as the client (relying party) and the tenant's own provider
(Google, Microsoft, Okta, Auth0, Keycloak, ...) configured per tenant,
same lazy-created-config pattern as LDAP and SAML (models.OAuthConfig).

What's real here, not stubbed:

  * The code-for-token exchange is a genuine `httpx.post` to the
    provider's token endpoint.
  * The ID token is verified with python-jose (already a dependency, used
    for Slate's own session JWTs) against the provider's JWKS, fetched
    live from `jwks_uri` on every sign-in — never cached, so a provider's
    key rotation takes effect immediately instead of after a TTL. Checks:
    signature, `iss`, `aud`, `exp`, `iat`, `sub` all required and
    verified; algorithm allow-list (asymmetric only — an `alg: none` or
    HS256-signed-with-the-public-key token is rejected before any key is
    touched); key chosen by `kid`, never "try every key until one works";
    `nonce` compared against the one this sign-in started with.
  * `state` lives in Redis, single-use and short-lived, and is also bound
    to the browser that started the flow by an HttpOnly cookie (see the
    router) — so a callback can't be replayed, and can't be forged by
    someone else's flow either (login CSRF).
  * PKCE (S256) is sent on top of the client secret. Providers that don't
    support it ignore the parameters (RFC 6749 §3.1); providers that do
    get the extra protection against a stolen authorization code.

Like ldap_auth.py and saml_auth.py, every failure mode ends in one
exception type (OAuthError) with a message that is safe to show the
person signing in — never a secret, never a raw provider payload.

Verified end to end during the build against an independently-written
test provider that signs with a *different* JWT library (joserfc) than
this module verifies with — see backend/scripts/verify_oauth.py.
"""

import base64
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlencode, urlparse

import httpx
import redis
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError

logger = logging.getLogger("slate.oauth")

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

STATE_TTL_SECONDS = 600  # long enough to type a password + MFA code at the provider; short enough that a stale link is useless
BINDING_COOKIE_PREFIX = "slate_oauth_"
BINDING_COOKIE_PATH = "/api/auth/oauth"
CLOCK_SKEW_SECONDS = 60
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_RESPONSE_BYTES = 1_000_000

# Asymmetric only. HS* is excluded on purpose (algorithm-confusion attack:
# an attacker signs with HMAC using the provider's *public* key as the
# secret), and so is "none". python-jose 3.3 doesn't implement PS*.
ALLOWED_ID_TOKEN_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512"]

_STATE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
_PROVIDER_ERROR_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class OAuthError(Exception):
    """Any failure in the sign-in. str(e) is safe to show the person."""


class TokenExchangeError(OAuthError):
    pass


class IdTokenError(OAuthError):
    pass


@dataclass
class OAuthIdentity:
    subject: str
    email: str
    name: str


# ---------------------------------------------------------------------------
# URLs and config validation
# ---------------------------------------------------------------------------

def redirect_uri(tenant_slug: str) -> str:
    """The exact string registered at the provider, and sent on both the
    authorization request and the token request (providers compare it
    byte for byte). The slug is percent-encoded — signup derives it from
    a free-text company name."""
    return f"{APP_BASE_URL}/api/auth/oauth/callback?tenant_slug={quote(tenant_slug, safe='')}"


def _is_production() -> bool:
    return os.getenv("ENVIRONMENT", "development").lower() == "production"


def validate_endpoint_url(label: str, url: str, server_fetched: bool = True) -> str:
    """Save-time validation for the URLs an org admin types in. Raises
    ValueError with a message safe to show back to them.

    token_endpoint and jwks_uri are requested *by this server* with
    whatever the admin configured, which makes them an SSRF vector (point
    one at a cloud metadata service or an internal admin panel). In
    production that means https only and no literal loopback/private/
    link-local addresses. Local development stays unrestricted so a
    provider on http://localhost works. Not covered, and worth knowing:
    a public hostname that *resolves* to a private address — closing that
    needs an egress proxy or DNS pinning, not a string check."""
    value = (url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"{label} must be a full http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{label} must not contain credentials")
    if parsed.fragment:
        raise ValueError(f"{label} must not contain a fragment")

    if _is_production():
        if parsed.scheme != "https":
            raise ValueError(f"{label} must use https in production")
        if server_fetched:
            host = parsed.hostname.lower()
            if host == "localhost" or host.endswith(".localhost"):
                raise ValueError(f"{label} can't point at localhost in production")
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                ip = None
            if ip is not None and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                                   or ip.is_unspecified or ip.is_multicast):
                raise ValueError(f"{label} can't point at a private or loopback address in production")
    return value


def normalize_scopes(scopes: str) -> str:
    parts = (scopes or "").split()
    if "openid" not in parts:
        raise ValueError("Scopes must include 'openid' — without it the provider won't return an ID token")
    return " ".join(dict.fromkeys(parts))  # de-duplicated, order kept


# ---------------------------------------------------------------------------
# Redis-backed state (CSRF), same fail-fast contract as webauthn_auth.py
# ---------------------------------------------------------------------------

_redis = None
_REDIS_DOWN = "Redis is required for OAuth sign-in (CSRF state) but isn't reachable"


def _get_redis():
    """Connects on first use and keeps retrying while Redis is down
    (1-second timeouts, so a down Redis is still a *fast* failure).
    webauthn_auth.py decides once at import time, which means a Redis
    that comes up a few seconds after the app does stays 'down' until
    the next restart; checking at use time costs nothing and recovers on
    its own."""
    global _redis
    if _redis is None:
        try:
            client = redis.from_url(REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
            client.ping()
            _redis = client
        except Exception:
            return None
    return _redis


def store_state(data: dict) -> str:
    """Unlike cache.py's cache_set, this does NOT silently no-op when
    Redis is unavailable — a sign-in that redirected the browser to the
    provider without saving its state could never complete, and would
    fail confusingly at the callback instead of clearly at the start.
    Raises RuntimeError; the caller turns it into one clear 503."""
    client = _get_redis()
    if client is None:
        raise RuntimeError(_REDIS_DOWN)
    state_id = secrets.token_urlsafe(32)
    try:
        client.setex(f"oauth:state:{state_id}", STATE_TTL_SECONDS, json.dumps(data))
    except redis.RedisError as e:
        raise RuntimeError(_REDIS_DOWN) from e
    return state_id


def pop_state(state_id: str) -> Optional[dict]:
    """Single-use: read-and-delete in one MULTI/EXEC transaction, so two
    simultaneous callbacks carrying the same state can't both win (a
    plain GET followed by DEL leaves a window between them). Returns None
    for unknown, expired, already-used, or malformed state. Raises
    RuntimeError if Redis itself is unreachable — distinguishable from
    'invalid state' so the person sees 'try again shortly', not 'your
    link expired'."""
    if not state_id or not _STATE_ID_RE.match(state_id):
        return None
    client = _get_redis()
    if client is None:
        raise RuntimeError(_REDIS_DOWN)
    key = f"oauth:state:{state_id}"
    try:
        pipe = client.pipeline(transaction=True)
        pipe.get(key)
        pipe.delete(key)
        raw, _deleted = pipe.execute()
    except redis.RedisError as e:
        raise RuntimeError(_REDIS_DOWN) from e
    return json.loads(raw) if raw else None


def new_flow_secrets() -> dict:
    """Everything a sign-in attempt needs to remember between the
    redirect out and the callback back: the OIDC nonce, the PKCE
    verifier, and the hash of the browser-binding cookie's value."""
    code_verifier = secrets.token_urlsafe(64)  # 86 chars, inside RFC 7636's 43-128
    binding = secrets.token_urlsafe(24)
    return {
        "nonce": secrets.token_urlsafe(24),
        "code_verifier": code_verifier,
        "code_challenge": pkce_challenge(code_verifier),
        "binding": binding,  # raw value goes in the cookie; only its hash is stored
        "binding_hash": _sha256_hex(binding),
    }


def pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def binding_cookie_name(state_id: Optional[str]) -> Optional[str]:
    """One binding cookie per sign-in *attempt*, named after a prefix of
    its state, rather than one shared cookie per browser. A shared name
    means a second tab, a double click, or a retry after the back button
    silently overwrites the first attempt's binding and breaks it with a
    confusing "wasn't started in this browser" — and a stale, rejected
    callback would clear the cookie a live attempt still needs. The name
    is only ever built from a well-formed state, so nothing a caller
    sends can reach a Set-Cookie header."""
    if not state_id or not _STATE_ID_RE.match(state_id):
        return None
    return f"{BINDING_COOKIE_PREFIX}{state_id[:16]}"


def binding_matches(cookie_value: Optional[str], expected_hash: str) -> bool:
    if not cookie_value or not expected_hash:
        return False
    return secrets.compare_digest(_sha256_hex(cookie_value).encode(), expected_hash.encode())


# ---------------------------------------------------------------------------
# The flow itself
# ---------------------------------------------------------------------------

def build_authorization_url(config, tenant_slug: str, state: str, nonce: str, code_challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri(tenant_slug),
        "scope": config.scopes,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    separator = "&" if "?" in config.authorization_endpoint else "?"
    # quote (not quote_plus): scope separators go out as %20, which every provider accepts
    return f"{config.authorization_endpoint}{separator}{urlencode(params, quote_via=quote)}"


def _json_or_none(resp: httpx.Response) -> Optional[dict]:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _client_auth_rejected(resp: httpx.Response) -> bool:
    if resp.status_code == 401:
        return True
    body = _json_or_none(resp)
    return resp.status_code == 400 and bool(body) and body.get("error") == "invalid_client"


def exchange_code(config, client_secret: str, code: str, tenant_slug: str, code_verifier: str) -> str:
    """The real token request: POST to the provider's token endpoint with
    grant_type=authorization_code. Returns the raw ID token string.

    Client authentication goes in an HTTP Basic header first — the one
    method RFC 6749 §2.3.1 says every server MUST support, and the OIDC
    default. Providers differ on what they accept beyond that (Auth0's
    default app setting is client_secret_post, Okta's is Basic), so if
    the provider answers 401 / invalid_client, it's retried once with
    the credentials in the form body instead. That retry is safe: a
    failed client authentication never consumes the authorization code.
    """
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(tenant_slug),
        "code_verifier": code_verifier,
    }
    basic = base64.b64encode(
        f"{quote(config.client_id, safe='')}:{quote(client_secret, safe='')}".encode()
    ).decode()

    def post(use_basic: bool) -> httpx.Response:
        data = dict(form)
        headers = {"Accept": "application/json"}
        if use_basic:
            headers["Authorization"] = f"Basic {basic}"
        else:
            data["client_id"] = config.client_id
            data["client_secret"] = client_secret
        return httpx.post(config.token_endpoint, data=data, headers=headers, timeout=HTTP_TIMEOUT, follow_redirects=False)

    try:
        resp = post(use_basic=True)
        if _client_auth_rejected(resp):
            resp = post(use_basic=False)
    except httpx.HTTPError as e:
        logger.warning("oauth token endpoint unreachable for %s: %s", config.provider_name, type(e).__name__)
        raise TokenExchangeError("Couldn't reach the provider's token endpoint") from e

    if len(resp.content) > MAX_RESPONSE_BYTES:
        raise TokenExchangeError("The provider's token response was unexpectedly large")
    body = _json_or_none(resp)
    if body is None:
        logger.warning("oauth token endpoint returned non-JSON (HTTP %s) for %s", resp.status_code, config.provider_name)
        raise TokenExchangeError(f"The provider's token endpoint returned an unreadable response (HTTP {resp.status_code})")
    if resp.status_code != 200 or "error" in body:
        error = str(body.get("error", "unknown_error"))
        error = error if _PROVIDER_ERROR_RE.match(error) else "unknown_error"
        logger.warning("oauth token exchange rejected by %s: HTTP %s %s", config.provider_name, resp.status_code, error)
        raise TokenExchangeError(f"The provider rejected the sign-in code ({error})")

    id_token = body.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise TokenExchangeError(
            "The provider's response didn't include an ID token — check that the 'openid' scope is configured and granted"
        )
    return id_token


def fetch_jwks(jwks_uri: str) -> dict:
    """Live-fetched on every sign-in, deliberately uncached. One extra
    request per login is the price of key rotation (and key revocation)
    taking effect immediately."""
    try:
        resp = httpx.get(jwks_uri, headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT, follow_redirects=False)
        resp.raise_for_status()
        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise ValueError("JWKS response too large")
        jwks = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("oauth JWKS fetch failed: %s", type(e).__name__)
        raise IdTokenError("Couldn't fetch the provider's signing keys (JWKS)") from e
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if not isinstance(keys, list) or not keys:
        raise IdTokenError("The provider's JWKS didn't contain any signing keys")
    return jwks


def _select_key(jwks: dict, header: dict) -> dict:
    """Pick the one key the token says it was signed with. python-jose
    will happily try every key in a JWKS until one verifies; choosing by
    `kid` ourselves means a token can't be 'verified' by some unrelated
    key the provider also publishes."""
    candidates = [k for k in jwks["keys"] if isinstance(k, dict) and k.get("use", "sig") == "sig"]
    kid = header.get("kid")
    if kid is not None:
        matches = [k for k in candidates if k.get("kid") == kid]
        if not matches:
            raise IdTokenError("The ID token was signed with a key the provider doesn't publish")
        key = matches[0]
    else:
        if len(candidates) != 1:
            raise IdTokenError("The ID token names no signing key and the provider publishes several")
        key = candidates[0]
    if key.get("alg") and key["alg"] != header.get("alg"):
        raise IdTokenError("The ID token's algorithm doesn't match the provider's published key")
    return key


def verify_id_token(id_token: str, config, jwks: dict, expected_nonce: str) -> dict:
    """Real cryptographic verification with python-jose against the
    provider's published key. Returns the verified claims, or raises
    IdTokenError. python-jose treats exp/iss/aud/sub as optional unless
    told otherwise, so every one is explicitly required here — a token
    that simply omits `exp` must not sail through as 'never expires'.

    at_hash verification is switched off because Slate never uses the
    access token: identity comes only from this signed ID token, received
    directly from the token endpoint over TLS. (Left on, python-jose
    rejects any ID token carrying an at_hash — Google's, for one — unless
    given an access token to compare it with.)"""
    try:
        header = jwt.get_unverified_header(id_token)
    except JWTError as e:
        raise IdTokenError("The ID token is malformed") from e
    if header.get("alg") not in ALLOWED_ID_TOKEN_ALGS:
        raise IdTokenError(f"The ID token uses an unsupported signing algorithm ({str(header.get('alg'))[:20]!r})")

    try:
        unverified = jwt.get_unverified_claims(id_token)
    except JWTError as e:
        raise IdTokenError("The ID token is malformed") from e
    for required in ("iss", "sub", "aud", "exp", "iat"):
        # Only an early, honest error message — nothing is trusted from unverified claims. python-jose's
        # own require_* options below still enforce this after the signature check.
        if required not in unverified:
            raise IdTokenError(f"The ID token is missing a required claim ({required})")

    key = _select_key(jwks, header)
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=ALLOWED_ID_TOKEN_ALGS,
            audience=config.client_id,
            issuer=config.issuer,
            options={
                "require_exp": True, "require_iat": True, "require_iss": True,
                "require_aud": True, "require_sub": True,
                "verify_at_hash": False,
                "leeway": CLOCK_SKEW_SECONDS,
            },
        )
    except ExpiredSignatureError as e:
        raise IdTokenError("The ID token has expired") from e
    except JWTClaimsError as e:
        raise IdTokenError(f"The ID token was rejected: {e}") from e
    except JWTError as e:
        raise IdTokenError("The ID token's signature couldn't be verified") from e

    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise IdTokenError("The ID token has no subject")
    audience = claims.get("aud")
    if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != config.client_id:
        raise IdTokenError("The ID token has several audiences and wasn't issued to this client (azp)")

    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not secrets.compare_digest(token_nonce.encode(), expected_nonce.encode()):
        raise IdTokenError("The ID token doesn't belong to this sign-in attempt (nonce mismatch)")
    return claims


def identity_from_claims(claims: dict) -> OAuthIdentity:
    email = claims.get("email")
    if not isinstance(email, str) or "@" not in email or not email.strip():
        raise IdTokenError("The ID token didn't include an email address — check the 'email' scope is configured and granted")
    # Many providers (Azure AD among them) omit email_verified entirely, so
    # absence is accepted; an explicit "false" is not — an unverified
    # address is a claim, not proof, and email is how accounts are matched.
    verified = claims.get("email_verified")
    if verified is False or (isinstance(verified, str) and verified.strip().lower() == "false"):
        raise IdTokenError("The provider says this email address hasn't been verified")
    name = claims.get("name") or claims.get("preferred_username") or email
    return OAuthIdentity(subject=claims["sub"], email=email.strip(), name=str(name).strip() or email.strip())


def complete_login(config, client_secret: str, tenant_slug: str, code: str, flow: dict) -> OAuthIdentity:
    """code -> ID token -> verified claims -> identity. `flow` is the
    record popped from Redis at the callback."""
    id_token = exchange_code(config, client_secret, code, tenant_slug, flow["code_verifier"])
    jwks = fetch_jwks(config.jwks_uri)
    claims = verify_id_token(id_token, config, jwks, flow["nonce"])
    return identity_from_claims(claims)
