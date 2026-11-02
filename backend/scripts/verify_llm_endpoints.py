#!/usr/bin/env python3
"""
End-to-end verification of local and custom model providers, and of the outbound-request guard
(backend/outbound.py) that every tenant-controlled URL goes through.

    python backend/scripts/verify_llm_endpoints.py

Starts a private Redis, a mock OpenAI-compatible upstream, and TWO real backends — one in
development mode (LOCAL_LLM_BASE_URL set) and one with ENVIRONMENT=production — then checks, over
real HTTP, what actually reaches the upstream: the exact wire format, which auth header carried which
key, and (just as important) what did NOT arrive. See verify_common.py for the harness contract.
"""

import asyncio
import os
import secrets
import sys

import httpx
from cryptography.fernet import Fernet

import verify_common as v
from verify_common import MOCK_PORT, WORKDIR, Tenant, api, check, db_all, db_exec, db_one, section

PROD_PORT = 8767
PROD_DB = WORKDIR / "prod.db"
MOCK = f"http://127.0.0.1:{MOCK_PORT}"
KEY1, KEY2 = "sk-custom-SECRET-123", "azure-style-KEY-456"
TA = TB = TP = None
E = {}  # endpoint ids by short name


def mock_reset():
    httpx.post(f"{MOCK}/_reset", timeout=5)


def mock_log(path_prefix: str = "/v1/chat"):
    return [e for e in httpx.get(f"{MOCK}/_log", timeout=5).json() if e["path"].startswith(path_prefix)]


def copilot(tenant, text="hello-copilot"):
    return tenant.call("POST", "/api/copilot", json={"messages": [{"role": "user", "content": text}]})


def endpoint_payload(**over):
    return {"name": "Acme vLLM", "kind": "chat", "base_url": MOCK, "model": "acme-model", "api_key": KEY1, "auth_header": "Authorization", **over}


# ===========================================================================
@section("1. The 'local' provider (Ollama / vLLM / LM Studio via LOCAL_LLM_BASE_URL)")
def sec_local():
    settings = TA.call("GET", "/api/ai-studio/settings").json()
    local = next((p for p in settings["available_providers"] if p["key"] == "local"), None)
    check("'local' is offered in AI Studio, and shown as configured because the operator set LOCAL_LLM_BASE_URL", bool(local) and local["is_configured"] is True)
    check("the five hosted providers are all still there, unchanged",
          {"anthropic", "openai", "gemini", "grok", "deepseek"} <= {p["key"] for p in settings["available_providers"]})
    r = TA.call("PATCH", "/api/ai-studio/settings", json={"provider": "local"})
    check("selecting it works and uses the operator's LOCAL_LLM_MODEL as the default model", r.status_code == 200 and r.json()["provider"] == "local" and r.json()["model"] == "mock-local", r.text)

    mock_reset()
    r = copilot(TA, "hello-local-llm")
    check("AI Copilot is answered by the local server (not just 'configured' — a real round trip)",
          r.status_code == 200 and "MOCK-REPLY[mock-local]" in r.json()["reply"] and "hello-local-llm" in r.json()["reply"], r.text)
    req = (mock_log() or [None])[-1]
    check("wire format: POST /v1/chat/completions with model, OpenAI-style messages and max_tokens",
          bool(req) and req["body"]["model"] == "mock-local" and req["body"]["max_tokens"] == 1000
          and req["body"]["messages"][-1]["role"] == "user" and "hello-local-llm" in req["body"]["messages"][-1]["content"])
    check("no Authorization header was sent (the key is optional for a local server)", "authorization" not in req["headers"])
    check("only the one chat request reached the upstream", len(mock_log()) == 1)


# ===========================================================================
@section("2. Per-tenant custom endpoints: create, encrypt, select, call, test, rotate, delete")
def sec_custom():
    for bad, why in ((endpoint_payload(name=" "), "blank name"), (endpoint_payload(kind="image"), "unknown kind"),
                     (endpoint_payload(model=""), "blank model"), (endpoint_payload(auth_header="bad header!"), "malformed auth header name"),
                     (endpoint_payload(base_url="ftp://example.com"), "non-http(s) URL"), (endpoint_payload(base_url="http://user:pw@127.0.0.1:9300"), "credentials in the URL"),
                     (endpoint_payload(base_url="http://127.0.0.1:99999"), "out-of-range port")):
        check(f"validation: {why} → 400", TA.call("POST", "/api/ai-studio/endpoints", json=bad).status_code == 400)

    r = TA.call("POST", "/api/ai-studio/endpoints", json=endpoint_payload())
    check("a valid endpoint saves", r.status_code == 200 and r.json()["name"] == "Acme vLLM" and r.json()["has_api_key"] is True, r.text)
    E["acme"] = r.json()["id"]
    check("the response never contains the API key", KEY1 not in r.text and "api_key" not in r.json())
    check("duplicate name in the same workspace → 409", TA.call("POST", "/api/ai-studio/endpoints", json=endpoint_payload()).status_code == 409)

    raw = db_one("select api_key_encrypted from custom_model_endpoints where id = ?", (E["acme"],))
    import security
    check("raw SQLite read: the key is Fernet ciphertext, not the key", bool(raw) and KEY1 not in raw and raw.startswith("gAAAA"))
    check("…and security.decrypt_field recovers it exactly", security.decrypt_field(raw) == KEY1)
    audit = TA.call("GET", "/api/audit-log").json()
    check("creation was audited, and no audit entry (or list response) contains the key",
          any(e["action"] == "model_endpoint.created" for e in audit) and KEY1 not in str(audit) and KEY1 not in TA.call("GET", "/api/ai-studio/endpoints").text)

    settings = TA.call("GET", "/api/ai-studio/settings").json()
    check("it appears in AI Studio's provider list as a selectable, configured provider",
          any(p["key"] == f"custom:{E['acme']}" and p["is_configured"] and p["default_model"] == "acme-model" for p in settings["available_providers"]))
    r = TA.call("PATCH", "/api/ai-studio/settings", json={"provider": f"custom:{E['acme']}"})
    check("selecting it works, and its model becomes the workspace's model", r.status_code == 200 and r.json()["provider"] == f"custom:{E['acme']}" and r.json()["model"] == "acme-model", r.text)

    mock_reset()
    r = copilot(TA, "hello-custom-endpoint")
    req = (mock_log() or [None])[-1]
    check("Copilot is answered by the tenant's own server", r.status_code == 200 and "MOCK-REPLY[acme-model]" in r.json()["reply"] and "hello-custom-endpoint" in r.json()["reply"], r.text)
    check("a bare host was normalized to /v1/chat/completions, and the key went out as 'Authorization: Bearer …'",
          bool(req) and req["path"] == "/v1/chat/completions" and req["headers"].get("authorization") == f"Bearer {KEY1}")

    # an Azure-style gateway: raw key in a custom header, full URL pasted
    r = TA.call("POST", "/api/ai-studio/endpoints", json=endpoint_payload(name="Gateway", base_url=f"{MOCK}/v1/chat/completions", model="gw-model", api_key=KEY2, auth_header="api-key"))
    E["gateway"] = r.json()["id"]
    TA.call("PATCH", "/api/ai-studio/settings", json={"provider": f"custom:{E['gateway']}"})
    mock_reset()
    copilot(TA)
    req = (mock_log() or [None])[-1]
    check("custom auth header: the raw key goes in 'api-key', and NO Authorization header is sent (the other endpoint's key doesn't leak across)",
          bool(req) and req["headers"].get("api-key") == KEY2 and "authorization" not in req["headers"] and KEY1 not in str(req))

    # test button
    t = TA.call("POST", f"/api/ai-studio/endpoints/{E['acme']}/test").json()
    check("'Test' does a real round trip and reports latency + the model's reply", t["ok"] is True and "MOCK-REPLY" in t["reply"] and t["latency_ms"] >= 0, str(t))
    for name, model, expect in (("Broken", "fail-500", "HTTP 500"), ("WrongShape", "bad-shape", "OpenAI chat-completions format")):
        e = TA.call("POST", "/api/ai-studio/endpoints", json=endpoint_payload(name=name, model=model)).json()
        t = TA.call("POST", f"/api/ai-studio/endpoints/{e['id']}/test").json()
        check(f"'Test' on a misbehaving server ({model}) → ok:false with a useful reason, still HTTP 200, and no key in it",
              t["ok"] is False and expect in t["error"] and KEY1 not in str(t), str(t))
        E[name] = e["id"]
    check("an unreachable server → ok:false 'couldn't connect', not a 500",
          (lambda e: TA.call("POST", f"/api/ai-studio/endpoints/{e['id']}/test").json())(
              TA.call("POST", "/api/ai-studio/endpoints", json=endpoint_payload(name="Dead", base_url="http://127.0.0.1:9", model="x")).json())["ok"] is False)

    # key rotation
    TA.call("PATCH", "/api/ai-studio/settings", json={"provider": f"custom:{E['acme']}"})
    r = TA.call("PATCH", f"/api/ai-studio/endpoints/{E['acme']}", json={"api_key": "rotated-key-789"})
    mock_reset()
    copilot(TA)
    req = (mock_log() or [None])[-1]
    check("rotating the key takes effect on the next call; the old key is no longer sent", r.status_code == 200 and req["headers"].get("authorization") == "Bearer rotated-key-789")

    # lifecycle guards
    check("disabling the ACTIVE provider's endpoint is refused (409) instead of silently breaking Copilot",
          TA.call("PATCH", f"/api/ai-studio/endpoints/{E['acme']}", json={"enabled": False}).status_code == 409)
    check("deleting the ACTIVE provider's endpoint is refused (409)", TA.call("DELETE", f"/api/ai-studio/endpoints/{E['acme']}").status_code == 409)
    TA.call("PATCH", "/api/ai-studio/settings", json={"provider": "local"})
    r = TA.call("PATCH", f"/api/ai-studio/endpoints/{E['acme']}", json={"enabled": False})
    check("once another provider is active it can be disabled", r.status_code == 200 and r.json()["enabled"] is False)
    check("…a disabled endpoint disappears from the provider list and can't be selected",
          all(p["key"] != f"custom:{E['acme']}" for p in TA.call("GET", "/api/ai-studio/settings").json()["available_providers"])
          and TA.call("PATCH", "/api/ai-studio/settings", json={"provider": f"custom:{E['acme']}"}).status_code == 400)
    check("deleting it works now", TA.call("DELETE", f"/api/ai-studio/endpoints/{E['acme']}").status_code == 200)
    check("unknown provider names are still rejected (400)", TA.call("PATCH", "/api/ai-studio/settings", json={"provider": "nope"}).status_code == 400)


# ===========================================================================
@section("3. Workspace isolation and roles")
def sec_isolation():
    ids = [E["gateway"], E["Broken"]]
    check("workspace B sees none of workspace A's endpoints", TB.call("GET", "/api/ai-studio/endpoints").json() == [])
    check("B can't update, delete, or test A's endpoint by id (404 each)",
          TB.call("PATCH", f"/api/ai-studio/endpoints/{ids[0]}", json={"enabled": False}).status_code == 404
          and TB.call("DELETE", f"/api/ai-studio/endpoints/{ids[0]}").status_code == 404
          and TB.call("POST", f"/api/ai-studio/endpoints/{ids[0]}/test").status_code == 404)
    check("B can't select A's endpoint as its provider (400)", TB.call("PATCH", "/api/ai-studio/settings", json={"provider": f"custom:{ids[0]}"}).status_code == 400)

    # Even a corrupted/tampered row can't make B's calls reach A's server (or carry A's key)
    db_exec("insert into ai_provider_configs (tenant_id, provider, model) values (?, ?, ?)", (TB.id, f"custom:{ids[0]}", None))
    mock_reset()
    r = copilot(TB)
    check("TAMPERED ROW: B's provider row names A's endpoint → fails closed with a clear error, no server is called, A's key never leaves",
          r.status_code == 502 and "doesn't belong to this workspace" in r.text and mock_log("/") == [], f"{r.status_code} {r.text}")

    standard, _ = TA.add_user("standard_user")
    manager, _ = TA.add_user("manager")
    check("standard_user and manager can't list or create endpoints (org_admin only: they hold URLs and keys)",
          all(api("GET", "/api/ai-studio/endpoints", tok).status_code == 403 for tok in (standard, manager))
          and all(api("POST", "/api/ai-studio/endpoints", tok, json=endpoint_payload(name="x")).status_code == 403 for tok in (standard, manager)))
    check("anonymous callers get 401", api("GET", "/api/ai-studio/endpoints").status_code == 401)


# ===========================================================================
@section("4. outbound.py: the guard every tenant-controlled URL goes through")
def sec_outbound():
    import outbound

    def blocked(url, **kw):
        try:
            asyncio.run(outbound.check_url(url, **kw))
            return False
        except outbound.OutboundError:
            return True

    previous = {k: os.environ.get(k) for k in ("ENVIRONMENT", "ALLOW_PRIVATE_OUTBOUND")}
    os.environ["ENVIRONMENT"] = "production"
    os.environ.pop("ALLOW_PRIVATE_OUTBOUND", None)
    try:
        for url, what in (("http://127.0.0.1:9300/", "loopback"), ("http://localhost:9300/", "localhost"), ("http://[::1]/", "IPv6 loopback"),
                          ("http://169.254.169.254/latest/meta-data/", "cloud metadata service"), ("http://10.1.2.3/", "10/8"),
                          ("http://192.168.0.1/", "192.168/16"), ("http://172.16.5.5/", "172.16/12"), ("http://100.64.0.1/", "carrier-grade NAT (100.64/10)"),
                          ("http://0.0.0.0/", "0.0.0.0"), ("http://2130706433/", "127.0.0.1 written as a decimal integer"),
                          ("http://0x7f.0.0.1/", "127.0.0.1 written in hex"), ("http://[::ffff:127.0.0.1]/", "IPv4-mapped IPv6 loopback")):
            check(f"production: {what} is refused ({url})", blocked(url))
        check("production: a public address is accepted", not blocked("http://93.184.216.34/x"))
        os.environ["ALLOW_PRIVATE_OUTBOUND"] = "true"
        check("production + ALLOW_PRIVATE_OUTBOUND=true (self-hosted on-prem models): internal addresses are accepted", not blocked("http://10.1.2.3/") and not blocked("http://127.0.0.1:9300/"))
    finally:
        for k, val in previous.items():
            os.environ.pop(k, None) if val is None else os.environ.__setitem__(k, val)

    check("syntax: file:// javascript: ftp:// credentials fragment bad port are all refused",
          all(blocked(u) for u in ("file:///etc/passwd", "javascript:alert(1)", "ftp://example.com/", "http://a:b@example.com/", "http://example.com/#x", "http://example.com:99999/")))

    async def go():
        ok = await outbound.safe_request("GET", f"http://localhost:{MOCK_PORT}/echo", headers={"Host": "evil.example"})
        host_seen = ok.json()["headers"]["host"]
        redirect = await outbound.safe_request("GET", f"{MOCK}/redirect")
        small = await outbound.safe_request("GET", f"{MOCK}/big?kb=10", max_bytes=100_000)
        errors = {}
        for label, coro in (("big", outbound.safe_request("GET", f"{MOCK}/big?kb=500", max_bytes=100_000)),
                            ("slow", outbound.safe_request("GET", f"{MOCK}/slow?seconds=3", timeout=1)),
                            ("refused", outbound.safe_request("GET", "http://127.0.0.1:9/x", timeout=2))):
            try:
                await coro
                errors[label] = None
            except outbound.OutboundError as e:
                errors[label] = str(e)
        return ok, host_seen, redirect, small, errors

    mock_reset()
    ok, host_seen, redirect, small, errors = asyncio.run(go())
    check("development: a hostname that resolves to IPv6 first (localhost) still connects — the pinned connection falls back across validated addresses", ok.status_code == 200)
    check("the Host header is the URL's own host (pinned to the checked IP, Host preserved; a caller can't override it)", host_seen == f"localhost:{MOCK_PORT}", host_seen)
    check("redirects are NOT followed (a public URL that 302s inward is the classic bypass): the 302 comes back as-is and the target was never requested",
          redirect.status_code == 302 and "location" in redirect.headers and not any(e["path"] == "/echo" and e["query"].get("redirected") for e in httpx.get(f"{MOCK}/_log").json()))
    check("a response under the cap comes back whole", small.status_code == 200 and len(small.content) == 10 * 1024)
    check("a response over the cap is cut off while streaming (500 KB vs a 100 KB cap)", bool(errors["big"]) and "larger than" in errors["big"], str(errors))
    check("a slow upstream times out with a clear message", bool(errors["slow"]) and "timed out" in errors["slow"], str(errors))
    check("a closed port is a clear 'couldn't connect', not a stack trace", bool(errors["refused"]) and "couldn't connect" in errors["refused"], str(errors))


# ===========================================================================
@section("5. Production-mode backend (ENVIRONMENT=production)")
def sec_production():
    def create(url, **over):
        return TP.call("POST", "/api/ai-studio/endpoints", json=endpoint_payload(name=f"p-{secrets.token_hex(3)}", base_url=url, **over))

    for url, what in ((MOCK, "a loopback model server"), ("http://169.254.169.254/latest", "the cloud metadata service"), ("https://localhost/v1", "localhost over https"),
                      ("http://10.0.0.5:8000", "a private-network address")):
        r = create(url)
        check(f"saving an endpoint at {what} → 400 'private or internal address'", r.status_code == 400 and "private or internal" in r.text, f"{r.status_code} {r.text}")
    r = create("http://93.184.216.34")
    check("a public address saves fine (the guard blocks internal targets, not external ones)", r.status_code == 200, r.text)

    TP.call("PATCH", "/api/ai-studio/settings", json={"provider": "local"})
    mock_reset()
    r = copilot(TP)
    check("provider 'local' with no LOCAL_LLM_BASE_URL on the server → clean 500 naming the missing variable, no network call at all",
          r.status_code == 500 and "LOCAL_LLM_BASE_URL is not configured" in r.text and mock_log("/") == [], f"{r.status_code} {r.text}")


# ===========================================================================
def setup():
    global TA, TB, TP
    v.start_redis()
    v.start_mock()
    v.start_slate(env={"LOCAL_LLM_BASE_URL": f"{MOCK}/v1", "LOCAL_LLM_MODEL": "mock-local"})
    v.start_slate(PROD_PORT, name="slate-prod", db_path=PROD_DB, env={
        "ENVIRONMENT": "production", "SECRET_KEY": secrets.token_urlsafe(32), "FIELD_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    })
    TA = Tenant("Verify LLM A", f"admin-a-{secrets.token_hex(3)}@example.com")
    TB = Tenant("Verify LLM B", f"admin-b-{secrets.token_hex(3)}@example.com")
    TP = Tenant("Verify LLM Prod", f"admin-p-{secrets.token_hex(3)}@example.com", port=PROD_PORT, db_path=PROD_DB)


if __name__ == "__main__":
    sys.exit(v.run("Local and custom model providers + outbound guard", setup, log_names=("slate", "slate-prod")))
