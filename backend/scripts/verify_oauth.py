#!/usr/bin/env python3
"""
End-to-end verification of Slate's OAuth 2.0 / OpenID Connect login.

One command starts everything it needs, runs every check against the
real running app over real HTTP, and tears it all down again:

    pip install joserfc          # the test provider's JWT library (not a Slate dependency)
    python backend/scripts/verify_oauth.py

  * a private Redis (needs the `redis-server` binary on PATH) on 127.0.0.1:6390
  * two independent test OIDC providers (oidc_test_provider.py — FastAPI +
    joserfc, a different JWT library from the python-jose Slate verifies with)
  * the real Slate backend (`uvicorn main:app`) on a throwaway SQLite file
  * a "browser": a cookie-keeping HTTP client that follows every redirect
    by hand, so each hop of the real flow is visible and assertable

Every child process is started in its own session (setsid), so nothing
depends on a tool call, shell, or terminal staying alive — and all of
them are killed again in `finally`, pass or fail. Exits 0 and prints ALL
PASSED, or exits 1 and prints exactly what failed (plus the tail of each
server log). Same contract as smoke_test.py.
"""

import base64
import hashlib
import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import redis

BACKEND = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

REDIS_PORT, LATE_REDIS_PORT = 6390, 6392
SLATE_PORT, SLATE2_PORT = 8765, 8766
IDP_A_PORT, IDP_B_PORT = 9100, 9101
SLATE = f"http://127.0.0.1:{SLATE_PORT}"
FRONTEND_URL = "http://127.0.0.1:3999"  # nothing listens here; we only read the redirect that points at it

IDP_PASSWORD = "correct-horse-battery"
ADMIN_PASSWORD = "verify-admin-pw-123"

WORKDIR = Path(tempfile.mkdtemp(prefix="slate-verify-oauth-"))
DB_PATH = WORKDIR / "slate_verify.db"

FAILURES: list = []
CHECKS = 0
PROCS: list = []
SECTIONS: list = []


# ---------------------------------------------------------------------------
# Tiny test framework — same shape as smoke_test.py's check()
# ---------------------------------------------------------------------------

def check(label: str, cond: bool, extra: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)
    return bool(cond)


def section(title: str):
    def register(fn):
        SECTIONS.append((title, fn))
        return fn
    return register


# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------

def spawn(name: str, cmd: list, env: dict = None, cwd=None) -> subprocess.Popen:
    log = open(WORKDIR / f"{name}.log", "wb")
    proc = subprocess.Popen(
        cmd, cwd=cwd, env={**os.environ, **(env or {})}, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True,  # setsid: its own process group, immune to whatever happens to our shell
    )
    PROCS.append((name, proc))
    return proc


def kill(proc: subprocess.Popen, sig=signal.SIGTERM) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), sig)
        proc.wait(timeout=10)
    except Exception:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            pass


def wait_until(fn, timeout: float = 30.0, what: str = "service") -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            if fn():
                return
        except Exception as e:  # noqa: BLE001 — anything means "not ready yet"
            last = e
        time.sleep(0.25)
    raise RuntimeError(f"{what} never became ready ({last})")


def start_redis(port: int) -> subprocess.Popen:
    proc = spawn(f"redis-{port}", ["redis-server", "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no", "--dir", str(WORKDIR)])
    wait_until(lambda: redis.Redis(port=port, socket_connect_timeout=1).ping(), 10, f"redis:{port}")
    return proc


def start_slate(port: int, redis_port: int, name: str) -> subprocess.Popen:
    env = {
        "DATABASE_URL": f"sqlite:///{DB_PATH}",
        "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
        "APP_BASE_URL": f"http://127.0.0.1:{port}",
        "FRONTEND_URL": FRONTEND_URL,
        "ALLOWED_ORIGINS": FRONTEND_URL,
    }
    proc = spawn(name, [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port),
                        "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1", "--log-level", "warning"],
                 env=env, cwd=BACKEND)
    wait_until(lambda: httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2).status_code == 200, 45, name)
    return proc


# ---------------------------------------------------------------------------
# The test providers
# ---------------------------------------------------------------------------

class IdP:
    def __init__(self, port: int, client_id: str, secret: str, auth_method: str):
        self.port, self.client_id, self.secret, self.auth_method = port, client_id, secret, auth_method
        self.base = f"http://127.0.0.1:{port}"
        self.jwks_uri = f"{self.base}/.well-known/jwks.json"

    def start(self) -> None:
        spawn(f"idp-{self.port}", [sys.executable, str(SCRIPTS / "oidc_test_provider.py")], env={
            "TEST_OIDC_PORT": str(self.port), "TEST_OIDC_CLIENT_ID": self.client_id,
            "TEST_OIDC_CLIENT_SECRET": self.secret, "TEST_OIDC_PASSWORD": IDP_PASSWORD,
            "TEST_OIDC_AUTH_METHOD": self.auth_method,
        })
        wait_until(lambda: httpx.get(f"{self.base}/_test/health", timeout=2).status_code == 200, 45, f"idp:{self.port}")

    def post(self, path: str, body: dict) -> dict:
        r = httpx.post(f"{self.base}{path}", json=body, timeout=10)
        r.raise_for_status()
        return r.json()

    def add_user(self, email: str, name: str, email_verified=True, sub=None) -> None:
        self.post("/_test/users", {"email": email, "name": name, "email_verified": email_verified, "sub": sub})

    def arm(self, mode: str, count: int = 1) -> None:
        self.post("/_test/arm", {"mode": mode, "count": count})

    def log(self) -> dict:
        return httpx.get(f"{self.base}/_test/log", timeout=10).json()

    def rotate(self) -> dict:
        return self.post("/_test/rotate-key", {})

    def disarm(self, mode: str) -> None:
        self.arm(mode, 0)

    def config_payload(self, secret=None, enabled=True, **over) -> dict:
        return {
            "provider_name": "Test IdP", "client_id": self.client_id, "client_secret": secret if secret is not None else self.secret,
            "authorization_endpoint": f"{self.base}/authorize", "token_endpoint": f"{self.base}/token",
            "jwks_uri": self.jwks_uri, "issuer": self.base, "scopes": "openid email profile", "enabled": enabled, **over,
        }


IDP_A = IdP(IDP_A_PORT, "slate-client-a", "secret-A-" + secrets.token_urlsafe(18), "both")
IDP_B = IdP(IDP_B_PORT, "slate-client-b", "secret-B-" + secrets.token_urlsafe(18), "post")  # only accepts client_secret_post


# ---------------------------------------------------------------------------
# HTTP helpers: API client and a hop-by-hop "browser"
# ---------------------------------------------------------------------------

_ip_counter = 0


def fresh_ip() -> str:
    """Each simulated person gets their own client IP (via X-Forwarded-For,
    which uvicorn trusts from loopback), exactly like users behind a real
    proxy — so the per-IP rate limits that protect production don't make
    one test starve the next. The rate limit itself gets its own test."""
    global _ip_counter
    _ip_counter += 1
    return f"10.77.{_ip_counter // 250}.{_ip_counter % 250 + 1}"


def api(method: str, path: str, token: str = None, ip: str = None, port: int = SLATE_PORT, **kw) -> httpx.Response:
    headers = {"X-Forwarded-For": ip or fresh_ip()}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.request(method, f"http://127.0.0.1:{port}{path}", headers=headers, timeout=30, **kw)


class Browser:
    """Cookie jar + manual redirects: what a browser does between a click
    and the page it lands on, with every hop exposed."""

    def __init__(self, ip: str = None):
        self.ip = ip or fresh_ip()
        self.client = httpx.Client(follow_redirects=False, timeout=30, headers={"X-Forwarded-For": self.ip})

    def get(self, url, **kw):
        return self.client.get(url, **kw)

    def post(self, url, **kw):
        return self.client.post(url, **kw)

    def set_cookie(self, name: str, value: str) -> None:
        self.client.cookies.set(name, value, domain="127.0.0.1", path="/api/auth/oauth")

    def cookie(self, name: str):
        return self.client.cookies.get(name)

    def binding_cookies(self) -> dict:
        return {c.name: c.value for c in self.client.cookies.jar if c.name.startswith("slate_oauth_")}


def binding_cookie_name(state: str) -> str:
    return f"slate_oauth_{state[:16]}"


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "input" and a.get("type") == "hidden" and a.get("name"):
            self.fields[a["name"]] = a.get("value", "")


def hidden_fields(html: str) -> dict:
    p = _FormParser()
    p.feed(html)
    return p.fields


def outcome_of(resp: httpx.Response) -> SimpleNamespace:
    """Reads the frontend hand-off the way LoginScreen.js does: from the fragment."""
    loc = resp.headers.get("location", "")
    if resp.status_code == 302 and loc.startswith(FRONTEND_URL + "/#"):
        frag = parse_qs(loc.split("#", 1)[1])
        for key, kind in (("oauth_token", "token"), ("oauth_mfa_token", "mfa"), ("oauth_error", "error")):
            if key in frag:
                return SimpleNamespace(kind=kind, value=frag[key][0], location=loc)
    return SimpleNamespace(kind="other", value=f"HTTP {resp.status_code} {loc or resp.text[:160]}", location=loc)


def begin(tenant, browser: Browser) -> httpx.Response:
    return browser.get(f"{SLATE}/api/auth/oauth/login", params={"tenant_slug": tenant.slug})


def authorize(idp: IdP, browser: Browser, authorize_url: str, email: str, password: str = IDP_PASSWORD, action: str = "approve"):
    page = browser.get(authorize_url)
    if page.status_code != 200:
        return page, None
    resp = browser.post(f"{idp.base}/authorize", data={**hidden_fields(page.text), "username": email, "password": password, "action": action})
    return page, resp


def callback(browser: Browser, url: str):
    resp = browser.get(url)
    return resp, outcome_of(resp)


def signin(tenant, idp: IdP, email: str, browser: Browser = None, password: str = IDP_PASSWORD, action: str = "approve") -> SimpleNamespace:
    """The complete redirect flow, start to finish, hop by hop."""
    b = browser or Browser()
    r1 = begin(tenant, b)
    res = SimpleNamespace(browser=b, login=r1, authorize_url=r1.headers.get("location", ""), callback_url=None, state=None, code=None,
                          cb=None, outcome=SimpleNamespace(kind="other", value=f"login → HTTP {r1.status_code} {r1.text[:120]}", location=""))
    if r1.status_code != 302:
        return res
    page, r3 = authorize(idp, b, res.authorize_url, email, password, action)
    if r3 is None or r3.status_code != 302:
        res.outcome = SimpleNamespace(kind="other", value=f"provider step → HTTP {(r3 or page).status_code}", location="")
        return res
    res.callback_url = r3.headers["location"]
    q = parse_qs(urlsplit(res.callback_url).query)
    res.state, res.code = (q.get("state") or [None])[0], (q.get("code") or [None])[0]
    res.cb, res.outcome = callback(b, res.callback_url)
    return res


# ---------------------------------------------------------------------------
# Workspaces and database helpers
# ---------------------------------------------------------------------------

class Tenant:
    def __init__(self, name: str, admin_email: str):
        self.name, self.admin_email = name, admin_email
        r = api("POST", "/api/auth/signup", json={"tenant_name": name, "email": admin_email, "password": ADMIN_PASSWORD, "name": f"{name} Admin"})
        assert r.status_code == 200, f"signup failed: {r.text}"
        self.token = r.json()["access_token"]
        cfg = api("GET", "/api/auth/oauth-config", self.token).json()
        self.redirect_uri = cfg["redirect_uri"]
        self.slug = parse_qs(urlsplit(self.redirect_uri).query)["tenant_slug"][0]
        self.id = db_one("select id from tenants where slug = ?", (self.slug,))

    def set_flag(self, on: bool = True) -> None:
        assert api("PATCH", "/api/feature-flags/enterprise_oauth", self.token, json={"enabled": on}).status_code == 200

    def configure(self, idp: IdP, **over) -> httpx.Response:
        return api("PUT", "/api/auth/oauth-config", self.token, json=idp.config_payload(**over))


def db_one(sql: str, params: tuple = ()):
    con = sqlite3.connect(DB_PATH)
    try:
        row = con.execute(sql, params).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def db_all(sql: str, params: tuple = ()):
    con = sqlite3.connect(DB_PATH)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def port_open(port: int) -> bool:
    import socket
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


REDIS_PROC = None


TA = TB = None  # the two workspaces, created in setup()


# ===========================================================================
# 1. Config API, feature flag, secret handling
# ===========================================================================

@section("1. Config API, feature flag default, secret encrypted at rest")
def sec_config():
    flags = api("GET", "/api/feature-flags", TA.token).json()
    oauth_flag = next((f for f in flags if f["key"] == "enterprise_oauth"), None)
    check("enterprise_oauth flag is registered and OFF by default for a brand-new workspace", oauth_flag is not None and oauth_flag["enabled"] is False)
    check("all eight flags still present — adding one displaced nothing",
          {f["key"] for f in flags} == {"realtime_updates", "plugin_system", "multi_agent_handoff", "white_label_branding",
                                        "enterprise_ldap", "enterprise_saml", "enterprise_oauth", "passkeys"})
    check("no feature_flags row was created for it (registry default, same as every other flag)",
          db_one("select count(*) from feature_flags where tenant_id = ? and key = 'enterprise_oauth'", (TA.id,)) == 0)

    cfg = api("GET", "/api/auth/oauth-config", TA.token).json()
    check("unconfigured workspace: defaults, no secret, and the exact redirect URI to register at the provider",
          cfg["is_configured"] is False and cfg["has_client_secret"] is False and cfg["scopes"] == "openid email profile"
          and cfg["redirect_uri"] == f"{SLATE}/api/auth/oauth/callback?tenant_slug={TA.slug}")
    check("config endpoints reject anonymous callers (401)",
          api("GET", "/api/auth/oauth-config").status_code == 401
          and api("PUT", "/api/auth/oauth-config", json=IDP_A.config_payload()).status_code == 401)

    r = Browser().get(f"{SLATE}/api/auth/oauth/login", params={"tenant_slug": TA.slug})
    check("flag OFF → /oauth/login refused server-side (403), not merely hidden in the UI", r.status_code == 403, r.text)
    r = Browser().get(f"{SLATE}/api/auth/oauth/login", params={"tenant_slug": "no-such-workspace"})
    check("unknown workspace → 404", r.status_code == 404)

    def put(**over):
        return api("PUT", "/api/auth/oauth-config", TA.token, json=IDP_A.config_payload(**over))

    check("scopes without 'openid' rejected (no ID token would ever come back)", put(scopes="email profile").status_code == 400)
    check("non-http(s) endpoint rejected", put(token_endpoint="javascript:alert(1)").status_code == 400)
    check("endpoint with embedded credentials rejected", put(jwks_uri="http://user:pw@127.0.0.1:9100/jwks").status_code == 400)
    check("blank client ID rejected", put(client_id="   ").status_code == 400)
    check("enabling without any client secret rejected", api("PUT", "/api/auth/oauth-config", TA.token,
          json={**IDP_A.config_payload(enabled=True), "client_secret": None}).status_code == 400)

    r = put(enabled=False)
    check("valid config saves (disabled first)", r.status_code == 200 and r.json()["is_configured"] and r.json()["has_client_secret"] is True, r.text)
    check("response exposes has_client_secret only — the secret appears nowhere in it", IDP_A.secret not in r.text)
    check("GET after save: still no secret anywhere", IDP_A.secret not in api("GET", "/api/auth/oauth-config", TA.token).text)

    raw = db_one("select client_secret_encrypted from oauth_configs where tenant_id = ?", (TA.id,))
    check("raw SQLite read: stored value is Fernet ciphertext, no trace of the plaintext secret",
          bool(raw) and IDP_A.secret not in raw and raw.startswith("gAAAA"))
    import security  # the real module: same encrypt_field/decrypt_field the LDAP password uses
    check("security.decrypt_field recovers the exact secret from what's stored", security.decrypt_field(raw) == IDP_A.secret)

    r = api("PUT", "/api/auth/oauth-config", TA.token, json={**IDP_A.config_payload(enabled=False, provider_name="Test IdP (renamed)"), "client_secret": None})
    check("re-saving without a secret keeps the stored one (and applies the other edits)",
          r.status_code == 200 and r.json()["provider_name"] == "Test IdP (renamed)"
          and db_one("select client_secret_encrypted from oauth_configs where tenant_id = ?", (TA.id,)) == raw)

    TA.set_flag(True)
    r = Browser().get(f"{SLATE}/api/auth/oauth/login", params={"tenant_slug": TA.slug})
    check("flag ON but config disabled → login refused (400)", r.status_code == 400, r.text)
    r = put(enabled=True, provider_name="Test IdP")
    check("config enabled", r.status_code == 200 and r.json()["enabled"] is True, r.text)

    audit = api("GET", "/api/audit-log", TA.token).json()
    check("every config save was audited, and no audit entry contains the secret",
          len([e for e in audit if e["action"] == "oauth_config.updated"]) >= 3 and IDP_A.secret not in json.dumps(audit))


# ===========================================================================
# 2. The complete redirect flow, hop by hop
# ===========================================================================

@section("2. Complete OAuth redirect flow, driven for real")
def sec_flow():
    r_state = redis.Redis(port=REDIS_PORT, decode_responses=True)
    b = Browser()
    r1 = begin(TA, b)
    loc = r1.headers.get("location", "")
    q = parse_qs(urlsplit(loc).query)
    check("hop 1: GET /api/auth/oauth/login → 302 to the provider's /authorize", r1.status_code == 302 and loc.startswith(f"{IDP_A.base}/authorize?"), f"{r1.status_code} {loc}")
    check("authorization request: response_type=code, client_id, the exact registered redirect_uri, requested scopes",
          q.get("response_type") == ["code"] and q.get("client_id") == [IDP_A.client_id]
          and q.get("redirect_uri") == [TA.redirect_uri] and q.get("scope") == ["openid email profile"], str(q))
    check("…plus an unguessable state, an OIDC nonce, and a PKCE S256 challenge",
          len(q.get("state", [""])[0]) >= 40 and len(q.get("nonce", [""])[0]) >= 20
          and q.get("code_challenge_method") == ["S256"] and len(q.get("code_challenge", [""])[0]) == 43)
    set_cookie = r1.headers.get("set-cookie", "")
    cookie_name = binding_cookie_name(q["state"][0])
    check("browser-binding cookie (one per attempt, named after its state): HttpOnly, SameSite=Lax, scoped to /api/auth/oauth",
          set_cookie.startswith(cookie_name + "=") and "httponly" in set_cookie.lower() and "samesite=lax" in set_cookie.lower()
          and "path=/api/auth/oauth" in set_cookie.lower(), set_cookie)
    check("login redirect is Cache-Control: no-store", r1.headers.get("cache-control") == "no-store")

    key = f"oauth:state:{q['state'][0]}"
    record_raw = r_state.get(key)
    ttl = r_state.ttl(key)
    record = json.loads(record_raw) if record_raw else {}
    check("state is in Redis with a TTL of at most 10 minutes", record_raw is not None and 0 < ttl <= 600, f"ttl={ttl}")
    check("stored record: tenant, nonce, PKCE verifier and the binding *hash* — never the cookie value itself",
          set(record) == {"tenant_slug", "nonce", "code_verifier", "binding_hash"} and record["tenant_slug"] == TA.slug
          and record["nonce"] == q["nonce"][0] and b.cookie(cookie_name) not in record_raw)
    check("the PKCE challenge on the wire is S256 of the verifier held server-side",
          b64url(hashlib.sha256(record["code_verifier"].encode()).digest()) == q["code_challenge"][0])

    page, r3 = authorize(IDP_A, b, loc, "alice@example.com")
    check("hop 2: provider renders its real sign-in form (200)", page.status_code == 200 and "Test provider sign-in" in page.text)
    cb_url = r3.headers.get("location", "")
    cq = parse_qs(urlsplit(cb_url).query)
    check("hop 3: provider authenticates the person and redirects to exactly the registered redirect_uri with code + the same state",
          r3.status_code == 302 and cb_url.startswith(TA.redirect_uri + "&") and cq.get("state") == q["state"] and bool(cq.get("code")), cb_url)

    cb, out = callback(b, cb_url)
    check("hop 4: callback → 302 to FRONTEND_URL/#oauth_token=… (fragment, matching SAML's #saml_token)",
          out.kind == "token" and cb.headers["location"].startswith(f"{FRONTEND_URL}/#oauth_token="), out.value)
    check("the token is never in a query string", "oauth_token" not in urlsplit(cb.headers["location"]).query)
    check("callback response is Cache-Control: no-store", cb.headers.get("cache-control") == "no-store")
    check("callback clears this attempt's binding cookie", f"{cookie_name}=" in cb.headers.get("set-cookie", "") and "max-age=0" in cb.headers.get("set-cookie", "").lower())
    check("state was consumed (gone from Redis)", r_state.get(key) is None)

    token = out.value
    me = api("GET", "/api/auth/me", token)
    check("the token is a working Slate session: /api/auth/me → Alice, standard_user, this workspace",
          me.status_code == 200 and me.json()["email"] == "alice@example.com" and me.json()["role"] == "standard_user"
          and me.json()["tenant"] == TA.name and me.json()["name"] == "Alice Anderson", me.text)
    check("…and works on an ordinary authenticated endpoint (/api/deals)", api("GET", "/api/deals", token).status_code == 200)

    role, verified = db_all("select role, email_verified from users where lower(email) = 'alice@example.com'")[0]
    check("exactly one local account was just-in-time provisioned, as standard_user, email_verified",
          db_one("select count(*) from users where lower(email) = 'alice@example.com'") == 1 and role == "standard_user" and verified == 1)
    check("the provisioned account has an unusable local password (password login with guesses fails)",
          api("POST", "/api/auth/login", json={"email": "alice@example.com", "password": "alice@example.com"}).status_code == 401)

    res2 = signin(TA, IDP_A, "alice@example.com")
    me2 = api("GET", "/api/auth/me", res2.outcome.value) if res2.outcome.kind == "token" else None
    check("second sign-in, same identity → same account, no duplicate",
          me2 is not None and me2.json()["id"] == me.json()["id"] and db_one("select count(*) from users where lower(email) = 'alice@example.com'") == 1)
    check("each flow gets a fresh state and nonce", res2.state != q["state"][0])

    check("JIT standard_user is refused on GET /oauth-config (403)", api("GET", "/api/auth/oauth-config", token).status_code == 403)
    check("…and on PUT /oauth-config (403)", api("PUT", "/api/auth/oauth-config", token, json=IDP_A.config_payload()).status_code == 403)

    actions = [e["action"] for e in api("GET", "/api/audit-log", TA.token).json()]
    check("audit log: user.provisioned_via_oauth once, auth.oauth_login for each sign-in",
          actions.count("user.provisioned_via_oauth") == 1 and actions.count("auth.oauth_login") >= 2, str(actions))

    ok_calls = [e for e in IDP_A.log()["log"] if e["ok"]]
    check("provider's own log: a real token request — HTTP Basic client auth + a verified PKCE verifier",
          bool(ok_calls) and ok_calls[0]["auth_method"] == "basic" and ok_calls[0]["pkce"] is True, str(ok_calls[:1]))

    bad = signin(TA, IDP_A, "alice@example.com", password="wrong-password")
    check("wrong password at the provider → no code is issued, Slate is never called back, no session",
          bad.callback_url is None and bad.outcome.kind == "other")
    denied = signin(TA, IDP_A, "alice@example.com", action="deny")
    check("person clicks Deny at the provider → back to the login screen with an error, no session",
          denied.outcome.kind == "error" and "access_denied" in denied.outcome.value, denied.outcome.value)
    replay = callback(Browser(), denied.callback_url)[1]
    check("a denied attempt's state is spent too (callback replay → expired/used)",
          replay.kind == "error" and "expired or was already used" in replay.value, replay.value)


# ===========================================================================
# 3. python-jose verifies a token signed by joserfc — independently of Slate's code path
# ===========================================================================

def direct_tokens(idp: IdP, redirect_uri: str, email: str, nonce: str = "direct-nonce-0001"):
    """Talks to the provider the way any OIDC client would — no Slate involved —
    and returns its raw token response plus the authorization code used."""
    verifier = secrets.token_urlsafe(64)
    b = Browser()
    params = {"response_type": "code", "client_id": idp.client_id, "redirect_uri": redirect_uri, "scope": "openid email profile",
              "state": "direct-state-" + secrets.token_hex(8), "nonce": nonce,
              "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256"}
    page = b.get(f"{idp.base}/authorize", params=params)
    resp = b.post(f"{idp.base}/authorize", data={**hidden_fields(page.text), "username": email, "password": IDP_PASSWORD, "action": "approve"})
    code = parse_qs(urlsplit(resp.headers["location"]).query)["code"][0]
    tok = httpx.post(f"{idp.base}/token", auth=(idp.client_id, idp.secret), timeout=15,
                     data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier})
    return tok, code, verifier


@section("3. python-jose independently verifies the joserfc-signed ID token")
def sec_jose():
    from jose import JWTError, jwt as jose_jwt
    from joserfc import jwt as rfc_jwt
    from joserfc.jwk import KeySet, RSAKey
    import oauth_auth

    tok, code, _ = direct_tokens(IDP_A, TA.redirect_uri, "alice@example.com")
    body = tok.json()
    check("provider /token returns a real token response with an id_token", tok.status_code == 200 and bool(body.get("id_token")) and body.get("token_type") == "Bearer", tok.text)
    id_token = body["id_token"]
    jwks = httpx.get(IDP_A.jwks_uri, timeout=10).json()

    header = jose_jwt.get_unverified_header(id_token)
    check("ID token is RS256, and its kid is one the provider publishes at /.well-known/jwks.json",
          header["alg"] == "RS256" and header["kid"] in {k["kid"] for k in jwks["keys"]}, str(header))

    claims = jose_jwt.decode(id_token, jwks, algorithms=["RS256"], audience=IDP_A.client_id, issuer=IDP_A.base,
                             access_token=body["access_token"])
    check("python-jose verifies the joserfc-signed token (signature, iss, aud, exp — and at_hash against the access token)", True)
    check("claims include iss, sub, aud, exp, email and name",
          all(k in claims for k in ("iss", "sub", "aud", "exp", "email", "name")) and claims["email"] == "alice@example.com" and claims["name"] == "Alice Anderson", str(claims))

    cross = rfc_jwt.decode(id_token, KeySet.import_key_set(jwks))
    check("cross-check: joserfc (the signer's own library) accepts it too", cross.claims["sub"] == claims["sub"])

    mine = oauth_auth.verify_id_token(id_token, SimpleNamespace(client_id=IDP_A.client_id, issuer=IDP_A.base), jwks, "direct-nonce-0001")
    check("Slate's own oauth_auth.verify_id_token accepts it with the right nonce", mine["sub"] == claims["sub"])

    def jose_rejects(token, **kw):
        # access_token supplied so python-jose's at_hash check passes — otherwise it rejects *every*
        # token carrying an at_hash, and a "rejected" result would prove nothing about the defect under test
        try:
            jose_jwt.decode(token, jwks, algorithms=["RS256"], audience=IDP_A.client_id, issuer=IDP_A.base,
                            access_token=body["access_token"], **kw)
            return False
        except JWTError:
            return True

    h, p, s = id_token.split(".")
    raw_sig = bytearray(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
    raw_sig[0] ^= 1
    check("control first: the untouched token passes python-jose under identical settings (so each rejection below is about its defect alone)", not jose_rejects(id_token))
    check("negative control: flip one signature bit → python-jose rejects it", jose_rejects(f"{h}.{p}.{b64url(bytes(raw_sig))}"))
    forged = rfc_jwt.encode({"alg": "RS256", "kid": header["kid"]}, claims, RSAKey.generate_key(2048, parameters={"kid": header["kid"]}))
    check("negative control: same claims, same kid, signed by a different key → python-jose rejects it", jose_rejects(forged))
    def jose_rejects_expectation(**expect):
        kw = {"audience": IDP_A.client_id, "issuer": IDP_A.base, **expect}
        try:
            jose_jwt.decode(id_token, jwks, algorithms=["RS256"], access_token=body["access_token"], **kw)
            return False
        except JWTError:
            return True

    check("negative control: expecting a different audience → python-jose rejects the same token", jose_rejects_expectation(audience="some-other-client"))
    check("negative control: expecting a different issuer → python-jose rejects the same token", jose_rejects_expectation(issuer="https://evil.example.com"))

    redeem = httpx.post(f"{IDP_A.base}/token", auth=(IDP_A.client_id, IDP_A.secret), timeout=15,
                        data={"grant_type": "authorization_code", "code": code, "redirect_uri": TA.redirect_uri, "code_verifier": "x" * 50})
    check("authorization code reused at the provider's /token → 400 invalid_grant", redeem.status_code == 400 and redeem.json()["error"] == "invalid_grant", redeem.text)
    check("provider flags the reuse as an attack (RFC 6749 §4.1.2)", code[:6] in IDP_A.log()["reused_codes"])
    wrong_secret = httpx.post(f"{IDP_A.base}/token", auth=(IDP_A.client_id, "not-the-secret"), timeout=15, data={"grant_type": "authorization_code", "code": "x"})
    check("wrong client secret → 401 invalid_client", wrong_secret.status_code == 401 and wrong_secret.json()["error"] == "invalid_client")


# ===========================================================================
# 4. Replay protection: codes, states, races, CSRF
# ===========================================================================

@section("4. Reused codes and reused states are rejected")
def sec_replay():
    r_state = redis.Redis(port=REDIS_PORT, decode_responses=True)

    # -- a code that was already redeemed, presented through a brand-new, legitimate flow ---------------
    first = signin(TA, IDP_A, "alice@example.com")
    check("setup: a normal sign-in succeeds and burns its authorization code", first.outcome.kind == "token")
    b = Browser()
    r1 = begin(TA, b)  # a fresh, valid state for this browser
    fresh_state = parse_qs(urlsplit(r1.headers["location"]).query)["state"][0]
    replay_url = f"{TA.redirect_uri}&code={first.code}&state={fresh_state}"
    cb, out = callback(b, replay_url)
    check("REUSED CODE (valid new state + already-redeemed code) → provider says invalid_grant, Slate issues no session",
          out.kind == "error" and "invalid_grant" in out.value, out.value)
    check("the failed exchange didn't leave a half-open session or leak a token", "oauth_token" not in cb.headers.get("location", ""))

    # -- a state that was already used ---------------------------------------------------------------
    s1 = signin(TA, IDP_A, "alice@example.com")
    check("setup: sign-in #1 succeeds (its state is now spent)", s1.outcome.kind == "token")
    b2 = Browser()
    r2 = begin(TA, b2)
    _, a2 = authorize(IDP_A, b2, r2.headers["location"], "alice@example.com")
    q2 = parse_qs(urlsplit(a2.headers["location"]).query)
    code2, state2 = q2["code"][0], q2["state"][0]

    token_calls_before = len(IDP_A.log()["log"])
    cb, out = callback(b2, f"{TA.redirect_uri}&code={code2}&state={s1.state}")
    check("REUSED STATE (spent state + a perfectly valid, unused code) → rejected", out.kind == "error" and "expired or was already used" in out.value, out.value)
    check("…rejected before any token exchange: the provider's own log shows no /token request was made",
          len(IDP_A.log()["log"]) == token_calls_before)
    cb, out = callback(b2, f"{TA.redirect_uri}&code={code2}&state={state2}")
    check("…and that unused code still works with its own state (the rejection didn't burn it)", out.kind == "token", out.value)
    cb, out = callback(b2, f"{TA.redirect_uri}&code={code2}&state={state2}")
    check("replaying the whole just-completed callback URL → rejected", out.kind == "error", out.value)

    check("unknown / forged state → rejected", callback(Browser(), f"{TA.redirect_uri}&code=abc&state={secrets.token_urlsafe(32)}")[1].kind == "error")
    check("missing state → rejected", callback(Browser(), f"{TA.redirect_uri}&code=abc")[1].kind == "error")
    check("malformed state (not a token we'd ever issue) → rejected",
          callback(Browser(), f"{TA.redirect_uri}&code=abc&state=%2A%2A%2Aknown*")[1].kind == "error")

    # -- two callbacks racing on the same state: exactly one may win ---------------------------------
    b3 = Browser()
    r3 = begin(TA, b3)
    _, a3 = authorize(IDP_A, b3, r3.headers["location"], "alice@example.com")
    url3 = a3.headers["location"]
    cookies = b3.binding_cookies()
    results, barrier = [], threading.Barrier(8)

    def racer():
        racer_browser = Browser(ip=b3.ip)
        for name, value in cookies.items():
            racer_browser.set_cookie(name, value)
        barrier.wait()
        results.append(callback(racer_browser, url3)[1].kind)

    threads = [threading.Thread(target=racer) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check("RACE: 8 simultaneous callbacks on one state → exactly one session issued (atomic single-use)",
          results.count("token") == 1 and results.count("error") == 7, str(results))

    # -- state belongs to one workspace -------------------------------------------------------------
    b4 = Browser()
    r4 = begin(TA, b4)
    _, a4 = authorize(IDP_A, b4, r4.headers["location"], "alice@example.com")
    q4 = parse_qs(urlsplit(a4.headers["location"]).query)
    cross = f"{TB.redirect_uri}&code={q4['code'][0]}&state={q4['state'][0]}"
    cb, out = callback(b4, cross)
    check("STATE FROM WORKSPACE A presented at WORKSPACE B's callback → rejected (different workspace)",
          out.kind == "error" and "different workspace" in out.value, out.value)
    check("…and that state is burned, so retrying at the right workspace fails too",
          callback(b4, a4.headers["location"])[1].kind == "error")

    # -- login CSRF: a flow started in one browser can't be finished in another ---------------------
    attacker = Browser()
    ra = begin(TA, attacker)
    _, aa = authorize(IDP_A, attacker, ra.headers["location"], "alice@example.com")  # attacker signs in as themselves…
    victim = Browser()                                                               # …and hands the callback URL to someone else
    cb, out = callback(victim, aa.headers["location"])
    check("LOGIN CSRF: valid state + valid code delivered by a *different browser* (no binding cookie) → rejected",
          out.kind == "error" and "wasn't started in this browser" in out.value and "oauth_token" not in cb.headers["location"], out.value)

    attacker2 = Browser()
    rb = begin(TA, attacker2)
    _, ab = authorize(IDP_A, attacker2, rb.headers["location"], "alice@example.com")
    victim2 = Browser()
    victim2.set_cookie(binding_cookie_name(parse_qs(urlsplit(ab.headers["location"]).query)["state"][0]), "a-cookie-the-attacker-guessed")
    check("…and a forged binding cookie doesn't help either", callback(victim2, ab.headers["location"])[1].kind == "error")

    # -- two tabs / a double click: concurrent attempts in one browser each keep their own binding -------
    tabs = Browser()
    t1, t2 = begin(TA, tabs), begin(TA, tabs)
    _, p1 = authorize(IDP_A, tabs, t1.headers["location"], "alice@example.com")
    _, p2 = authorize(IDP_A, tabs, t2.headers["location"], "alice@example.com")
    check("TWO TABS: the second sign-in attempt doesn't break the first (each attempt has its own binding cookie)",
          len(tabs.binding_cookies()) == 2)
    check("…tab 1 completes", callback(tabs, p1.headers["location"])[1].kind == "token")
    check("…and tab 2 completes too", callback(tabs, p2.headers["location"])[1].kind == "token")
    check("…and finishing them left no stale binding cookies behind", tabs.binding_cookies() == {})

    keys = r_state.keys("oauth:state:*")
    check("every state key in Redis carries a TTL — abandoned sign-ins can't pile up forever",
          bool(keys) and all(0 < r_state.ttl(k) <= 600 for k in keys), f"{len(keys)} keys, ttls={[r_state.ttl(k) for k in keys][:5]}")



# ===========================================================================
# 5. A hostile or broken provider: every defect the ID token can have
# ===========================================================================

ATTACKS = [
    # (armed defect, text the person sees, what the defect is)
    ("tamper_signature", "signature couldn't be verified", "one bit of the signature flipped"),
    ("tamper_payload", "signature couldn't be verified", "email swapped for the attacker's AFTER signing, original signature kept"),
    ("foreign_key", "signature couldn't be verified", "signed with an attacker's own key while claiming the provider's kid"),
    ("unknown_kid", "doesn't publish", "signed by the real key, but names a kid the provider doesn't publish"),
    ("alg_none", "unsupported signing algorithm", "unsecured JWT (alg=none, empty signature)"),
    ("hs256_confusion", "unsupported signing algorithm", "HS256 keyed with the provider's PUBLIC key (algorithm confusion)"),
    ("wrong_audience", "Invalid audience", "token issued to a different client"),
    ("multi_aud", "several audiences", "aud lists this client and another, with no azp"),
    ("wrong_issuer", "Invalid issuer", "iss is not the configured issuer"),
    ("expired", "expired", "exp an hour in the past"),
    ("missing_exp", "missing a required claim (exp)", "no exp claim at all"),
    ("wrong_nonce", "nonce mismatch", "nonce belongs to a different sign-in (token injection/replay)"),
    ("no_nonce", "nonce mismatch", "nonce omitted although one was sent"),
    ("no_email", "didn't include an email", "no email claim"),
    ("unverified_email", "hasn't been verified", "email_verified is false"),
    ("jwks_500", "signing keys", "JWKS endpoint is down → fails closed"),
    ("token_500", "unreadable response", "token endpoint answers 500 with a non-JSON body"),
]


@section("5. Forged, tampered and broken ID tokens are all rejected over the real HTTP path")
def sec_attacks():
    delivered_before = {e.get("mode") for e in IDP_A.log()["log"]}
    for mode, expect, what in ATTACKS:
        IDP_A.arm(mode)
        res = signin(TA, IDP_A, "carol@example.com")
        IDP_A.disarm(mode)
        check(f"{what} → no session, person is told why  [{mode}]",
              res.outcome.kind == "error" and expect in res.outcome.value, f"{res.outcome.kind}: {res.outcome.value}")
    delivered = {e.get("mode") for e in IDP_A.log()["log"]} - delivered_before
    token_level = {m for m, _, _ in ATTACKS if m not in ("jwks_500", "token_500")}
    check("provider's own log confirms every token defect was really delivered (the rejections weren't about something else)",
          token_level <= delivered, f"missing {token_level - delivered}")
    check("nothing was provisioned by any failed attempt: no carol, and no attacker@evil.example from the tampered payload",
          db_one("select count(*) from users where lower(email) in ('carol@example.com', 'attacker@evil.example')") == 0)
    ok = signin(TA, IDP_A, "carol@example.com")
    check("control: with no defect armed, the same person signs in fine (so the failures above were the defects, not a broken setup)",
          ok.outcome.kind == "token" and db_one("select count(*) from users where lower(email) = 'carol@example.com'") == 1, ok.outcome.value)


# ===========================================================================
# 6. JWKS is fetched live: rotation and revocation both take effect immediately
# ===========================================================================

@section("6. Live-fetched JWKS: key rotation and key revocation take effect immediately")
def sec_rotation():
    kid_before = IDP_A.log()["kid"]
    check("setup: sign-in works with the original key", signin(TA, IDP_A, "alice@example.com").outcome.kind == "token")
    rot = IDP_A.rotate()
    check("provider rotated its signing key", rot["old_kid"] == kid_before and rot["new_kid"] != kid_before)
    res = signin(TA, IDP_A, "alice@example.com")
    check("first sign-in after rotation succeeds with no restart, no cache TTL to wait out", res.outcome.kind == "token", res.outcome.value)
    signed_with = [e["kid"] for e in IDP_A.log()["log"] if e["ok"]][-1]
    check("…and that token really was signed by the NEW key", signed_with == rot["new_kid"])

    IDP_A.arm("retired_key")
    res = signin(TA, IDP_A, "alice@example.com")
    IDP_A.disarm("retired_key")
    check("a token signed by the OLD, now-withdrawn key (genuine signature, real kid) is rejected — revocation is immediate",
          res.outcome.kind == "error" and "doesn't publish" in res.outcome.value, res.outcome.value)
    check("control: honest sign-in right after still works", signin(TA, IDP_A, "alice@example.com").outcome.kind == "token")


# ===========================================================================
# 7. Client-auth fallback + workspace isolation
# ===========================================================================

@section("7. Provider that rejects HTTP Basic (client_secret_post only) and workspace isolation")
def sec_tenant_b():
    res = signin(TB, IDP_B, "bob@example.com")
    check("provider that only accepts client_secret_post: sign-in still completes", res.outcome.kind == "token", res.outcome.value)
    attempts = [(e["auth_method"], e["ok"]) for e in IDP_B.log()["log"]]
    check("provider's log: Basic rejected (401) first, then the same code redeemed with credentials in the body — and the code survived the failed attempt",
          attempts[:2] == [("basic", False), ("post", True)], str(attempts))
    check("…and the provider holds the authorization code single-use: replay → invalid_grant",
          httpx.post(f"{IDP_B.base}/token", data={"grant_type": "authorization_code", "code": res.code, "client_id": IDP_B.client_id,
                                                    "client_secret": IDP_B.secret, "redirect_uri": TB.redirect_uri, "code_verifier": "x" * 50},
                     timeout=10).json().get("error") == "invalid_grant")

    bob = api("GET", "/api/auth/me", res.outcome.value).json()
    check("workspace B's person lands in workspace B", bob["tenant"] == TB.name and bob["email"] == "bob@example.com" and bob["role"] == "standard_user")
    team = [m["email"] for m in api("GET", "/api/auth/team", res.outcome.value).json()]
    check("…and sees only workspace B's team (none of workspace A's people)", "bob@example.com" in team and "alice@example.com" not in team, str(team))
    check("workspace A's config and workspace B's config are independent rows", db_one("select count(*) from oauth_configs") == 2
          and db_one("select issuer from oauth_configs where tenant_id = ?", (TA.id,)) == IDP_A.base
          and db_one("select issuer from oauth_configs where tenant_id = ?", (TB.id,)) == IDP_B.base)

    IDP_B.add_user("alice@example.com", "Alice Impostor")  # same email as an existing account in workspace A
    clash = signin(TB, IDP_B, "alice@example.com")
    check("an email that already belongs to ANOTHER workspace → clear error, no session, no crash on the unique constraint",
          clash.outcome.kind == "error" and "different workspace" in clash.outcome.value, clash.outcome.value)
    check("…and nothing was created or touched for it", db_one("select count(*) from users where lower(email) = 'alice@example.com'") == 1
          and db_one("select tenant_id from users where lower(email) = 'alice@example.com'") == TA.id)


# ===========================================================================
# 8. The existing MFA branch is reused
# ===========================================================================

@section("8. MFA branch reused: #oauth_mfa_token, not #oauth_token")
def sec_mfa():
    import pyotp
    first = signin(TA, IDP_A, "mfa-user@example.com")
    secret = api("POST", "/api/auth/mfa/setup", first.outcome.value).json()["secret"]
    r = api("POST", "/api/auth/mfa/confirm", first.outcome.value, json={"code": pyotp.TOTP(secret).now()})
    check("setup: an OAuth-provisioned user turns MFA on through the existing endpoints", r.status_code == 200, r.text)

    res = signin(TA, IDP_A, "mfa-user@example.com")
    check("MFA on → callback redirects with #oauth_mfa_token — and there is no #oauth_token anywhere",
          res.outcome.kind == "mfa" and "oauth_token=" not in res.outcome.location, f"{res.outcome.kind} {res.outcome.location[:90]}")
    check("the MFA-pending token is NOT a session: refused as a Bearer token on /api/deals (401)", api("GET", "/api/deals", res.outcome.value).status_code == 401)
    check("a wrong TOTP code is refused (401)", api("POST", "/api/auth/mfa/verify-login", json={"mfa_token": res.outcome.value, "code": "000000"}).status_code == 401)
    r = api("POST", "/api/auth/mfa/verify-login", json={"mfa_token": res.outcome.value, "code": pyotp.TOTP(secret).now()})
    session = r.json().get("access_token") if r.status_code == 200 else None
    check("the existing /mfa/verify-login (unchanged) turns it into a real session", bool(session) and api("GET", "/api/deals", session).status_code == 200, r.text)


# ===========================================================================
# 9. Account matching
# ===========================================================================

@section("9. Account matching: case-insensitive, no duplicates, existing roles untouched")
def sec_matching():
    inv = api("POST", "/api/auth/invite", TA.token, json={"name": "Dana Local", "email": "dana@example.com", "password": "dana-local-pw-1", "role": "manager"})
    check("setup: an org admin invited dana@example.com as a manager", inv.status_code == 200, inv.text)
    IDP_A.add_user("Dana@Example.COM", "Dana Provider", email_verified=None)  # mixed case, and no email_verified claim at all (like Azure AD)
    res = signin(TA, IDP_A, "dana@example.com")
    me = api("GET", "/api/auth/me", res.outcome.value).json() if res.outcome.kind == "token" else {}
    check("provider says 'Dana@Example.COM' (no email_verified claim) → signs into the EXISTING dana@example.com account",
          res.outcome.kind == "token" and me.get("id") == inv.json()["id"], res.outcome.value)
    check("…keeps the role an admin gave them (manager, not downgraded to standard_user) and their own name",
          me.get("role") == "manager" and me.get("name") == "Dana Local", str(me))
    check("…and created no duplicate", db_one("select count(*) from users where lower(email) = 'dana@example.com'") == 1)

    IDP_A.add_user("Eve@Example.COM", "Eve Mixed", email_verified=None)
    res = signin(TA, IDP_A, "eve@example.com")
    check("first-time sign-in with a mixed-case email provisions the account under the lowercased address",
          res.outcome.kind == "token" and db_one("select email from users where lower(email) = 'eve@example.com'") == "eve@example.com")


# ===========================================================================
# 10. Kill switches
# ===========================================================================

@section("10. Kill switches work server-side, even for a sign-in already in flight")
def sec_kill():
    b = Browser()
    r1 = begin(TA, b)
    _, a = authorize(IDP_A, b, r1.headers["location"], "alice@example.com")
    TA.set_flag(False)
    cb = b.get(a.headers["location"])
    check("flag turned OFF mid-flow → the callback is refused (403): no session", cb.status_code == 403 and "location" not in cb.headers, f"{cb.status_code}")
    check("…and a fresh /oauth/login is refused too (403)", Browser().get(f"{SLATE}/api/auth/oauth/login", params={"tenant_slug": TA.slug}).status_code == 403)
    TA.set_flag(True)

    r = TA.configure(IDP_A, enabled=False)
    check("config disabled by an org admin", r.status_code == 200 and r.json()["enabled"] is False)
    check("…/oauth/login refused (400)", Browser().get(f"{SLATE}/api/auth/oauth/login", params={"tenant_slug": TA.slug}).status_code == 400)
    check("…and the callback refused (400)", Browser().get(f"{TA.redirect_uri}&code=x&state={secrets.token_urlsafe(32)}").status_code == 400)
    TA.configure(IDP_A, enabled=True)
    check("control: re-enabled, sign-in works again", signin(TA, IDP_A, "alice@example.com").outcome.kind == "token")


# ===========================================================================
# 11. Redis down: fail fast, fail closed, recover on its own
# ===========================================================================

@section("11. Redis down: fails fast and clearly (like the passkey endpoints), then recovers with no restart")
def sec_redis():
    global REDIS_PROC
    login_url = f"{SLATE}/api/auth/oauth/login"

    kill(REDIS_PROC)
    wait_until(lambda: not port_open(REDIS_PORT), 10, "redis to stop")
    t0 = time.time()
    r = Browser().get(login_url, params={"tenant_slug": TA.slug})
    elapsed = time.time() - t0
    check("Redis stopped mid-run: /oauth/login → 503 with the reason, NOT a redirect to a provider that could never call back",
          r.status_code == 503 and "Redis is required" in r.text and "location" not in r.headers, f"{r.status_code} {r.text[:120]}")
    check("…failing fast, not hanging (1-second Redis timeouts)", elapsed < 5, f"{elapsed:.1f}s")
    out = outcome_of(Browser().get(f"{TA.redirect_uri}&code=x&state={secrets.token_urlsafe(32)}"))
    check("…and a callback fails closed: back to the login screen with a clear error, no session",
          out.kind == "error" and "Redis is required" in out.value, f"{out.kind}: {out.value}")

    REDIS_PROC = start_redis(REDIS_PORT)
    r = Browser().get(login_url, params={"tenant_slug": TA.slug})
    check("Redis restarted: /oauth/login works again with NO app restart", r.status_code == 302, f"{r.status_code} {r.text[:100]}")
    check("…and a complete sign-in succeeds", signin(TA, IDP_A, "alice@example.com").outcome.kind == "token")

    # Booted while Redis was already down (webauthn_auth.py decides once, at import, and stays 'down' until restarted)
    start_slate(SLATE2_PORT, LATE_REDIS_PORT, "slate-late")
    r = api("GET", "/api/auth/oauth/login", port=SLATE2_PORT, params={"tenant_slug": TA.slug}, follow_redirects=False)
    check("app that BOOTED while Redis was down: /oauth/login → 503 with the reason", r.status_code == 503 and "Redis is required" in r.text, f"{r.status_code} {r.text[:100]}")
    start_redis(LATE_REDIS_PORT)
    r = api("GET", "/api/auth/oauth/login", port=SLATE2_PORT, params={"tenant_slug": TA.slug}, follow_redirects=False)
    check("…Redis comes up later → the same running process starts working (the check happens at use time, not once at import)",
          r.status_code == 302 and r.headers.get("location", "").startswith(IDP_A.base), f"{r.status_code} {r.text[:100]}")


# ===========================================================================
# 12. Neighbouring auth features still work next to it
# ===========================================================================

@section("12. Existing LDAP / SAML / passkey / password endpoints still work alongside it")
def sec_regression():
    check("password login (existing) still works", api("POST", "/api/auth/login", json={"email": TA.admin_email, "password": ADMIN_PASSWORD}).status_code == 200)
    ldap = api("GET", "/api/auth/ldap-config", TA.token)
    check("GET /ldap-config (existing)", ldap.status_code == 200 and ldap.json()["is_configured"] is False)
    saml = api("GET", "/api/auth/saml-config", TA.token)
    check("GET /saml-config (existing) — SP entity ID and ACS URL still built", saml.status_code == 200 and "/api/auth/saml/acs" in saml.json()["acs_url"])
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime
    idp_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "verify-saml-idp")])
    now = datetime.datetime.utcnow()
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(idp_key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30)).sign(idp_key, hashes.SHA256()))
    saved = api("PUT", "/api/auth/saml-config", TA.token, json={
        "idp_entity_id": "https://idp.verify.example/metadata", "idp_sso_url": "https://idp.verify.example/sso",
        "idp_x509_cert": cert.public_bytes(serialization.Encoding.PEM).decode(), "enabled": False})
    check("PUT /saml-config (existing) still validates a real X.509 cert and saves", saved.status_code == 200, saved.text[:150])
    meta = api("GET", "/api/auth/saml/metadata", params={"tenant_slug": TA.slug})
    check("SAML SP metadata (python3-saml/xmlsec, existing) still generates for a configured workspace",
          meta.status_code == 200 and b"EntityDescriptor" in meta.content, meta.text[:120])
    check("LDAP login (existing) is still gated by its own flag (403)", api("POST", "/api/auth/login-ldap", json={"tenant_slug": TA.slug, "username": "u", "password": "p"}).status_code == 403)
    check("SAML login (existing) is still gated by its own flag (403)", Browser().get(f"{SLATE}/api/auth/saml/login", params={"tenant_slug": TA.slug}).status_code == 403)
    reg = api("POST", "/api/auth/webauthn/register/begin", TA.token)
    check("passkey registration (existing, Redis-backed) still returns options and a challenge", reg.status_code == 200 and "challenge_id" in reg.json() and "options" in reg.json(), reg.text[:150])
    check("health and metrics endpoints", api("GET", "/api/health").status_code == 200 and "slate_http_requests_total" in api("GET", "/metrics").text)


# ===========================================================================
# 13. Endpoint validation (SSRF) and rate limiting
# ===========================================================================

@section("13. Endpoint validation (SSRF guard) and rate limiting")
def sec_guards():
    import oauth_auth

    def rejected(url, **kw):
        try:
            oauth_auth.validate_endpoint_url("URL", url, **kw)
            return False
        except ValueError:
            return True

    check("development: a provider on http://127.0.0.1 is accepted (this very run depends on it)", not rejected("http://127.0.0.1:9100/token"))
    previous = os.environ.get("ENVIRONMENT")
    os.environ["ENVIRONMENT"] = "production"
    try:
        check("production: plain http:// rejected", rejected("http://idp.example.com/token"))
        check("production: cloud metadata address (169.254.169.254) rejected as a server-fetched URL", rejected("https://169.254.169.254/latest/meta-data/"))
        check("production: localhost and loopback rejected", rejected("https://localhost/token") and rejected("https://127.0.0.1/token") and rejected("https://[::1]/jwks"))
        check("production: private ranges rejected", rejected("https://10.0.0.5/token") and rejected("https://192.168.1.10/jwks") and rejected("https://172.16.0.9/jwks"))
        check("production: an ordinary https provider URL is accepted", not rejected("https://login.example.com/oauth2/v2.0/token"))
        check("production: the browser-facing authorization endpoint only needs https — the server never fetches it",
              not rejected("https://sso.internal.example.com/authorize", server_fetched=False) and rejected("http://sso.example.com/authorize", server_fetched=False))
    finally:
        if previous is None:
            os.environ.pop("ENVIRONMENT", None)
        else:
            os.environ["ENVIRONMENT"] = previous

    ip = "10.250.250.250"
    codes = [Browser(ip=ip).get(f"{SLATE}/api/auth/oauth/login", params={"tenant_slug": TA.slug}).status_code for _ in range(66)]
    check("rate limit: 60 sign-in starts per minute per IP are served, then 429", codes[:60].count(302) == 60 and 429 in codes[60:], str(codes[55:]))


# ==== SECTIONS-END ====


# ===========================================================================
# Setup, teardown, entry point
# ===========================================================================

def setup() -> None:
    global TA, TB
    print(f"workdir: {WORKDIR}")
    global REDIS_PROC
    REDIS_PROC = start_redis(REDIS_PORT)
    IDP_A.start()
    IDP_B.start()
    start_slate(SLATE_PORT, REDIS_PORT, "slate")

    TA = Tenant("Verify Tenant A", f"admin-a-{secrets.token_hex(3)}@example.com")
    TB = Tenant("Verify Tenant B", f"admin-b-{secrets.token_hex(3)}@example.com")
    for tenant, idp in ((TA, IDP_A), (TB, IDP_B)):
        idp.post("/_test/redirect-uri", {"uri": tenant.redirect_uri})  # what an org admin does: copy it into the provider's app settings
    TB.set_flag(True)
    r = TB.configure(IDP_B)
    assert r.status_code == 200, r.text
    IDP_A.add_user("alice@example.com", "Alice Anderson")
    IDP_A.add_user("carol@example.com", "Carol Clark")
    IDP_A.add_user("mfa-user@example.com", "Mia Factor")
    IDP_B.add_user("bob@example.com", "Bob Brown")


def teardown() -> None:
    for _name, proc in reversed(PROCS):
        kill(proc)
    for name, proc in PROCS:
        if proc.poll() is None:
            kill(proc, signal.SIGKILL)


def main() -> int:
    if shutil.which("redis-server") is None:
        print("[FAIL] redis-server isn't on PATH — this check needs a real Redis (apt install redis-server / brew install redis)")
        return 2
    started = time.time()
    try:
        setup()
        for title, fn in SECTIONS:
            print(f"\n== {title} ==")
            try:
                fn()
            except Exception:  # noqa: BLE001 — a crashed section is a failure, and must not hide the sections after it
                traceback.print_exc()
                check(f"section crashed: {title}", False)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        check("setup", False)
    finally:
        teardown()

    print(f"\n{CHECKS} checks in {time.time() - started:.0f}s")
    if FAILURES:
        for name in ("slate", "slate-late"):
            log = WORKDIR / f"{name}.log"
            if log.exists():
                print(f"\n--- tail of {name}.log ---\n" + "\n".join(log.read_text(errors="replace").splitlines()[-25:]))
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        print(f"(logs kept in {WORKDIR})")
        return 1
    shutil.rmtree(WORKDIR, ignore_errors=True)
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
