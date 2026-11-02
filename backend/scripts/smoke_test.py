"""
A real smoke test against a running server: health check, signup+login,
one full CRUD round trip, and feature flags — enough to catch "the app
doesn't actually start" or "a core endpoint is broken" regressions.

This is not a replacement for the deeper end-to-end verification each
feature got while it was built (WebSocket delivery, Redis pub/sub, the
plugin loader, etc.) — it's the fast, cheap check that runs on every
push in CI (see .github/workflows/ci.yml) and every time a contributor
wants to sanity-check a change locally before pushing:

    python backend/scripts/smoke_test.py

Exits 0 and prints ALL PASSED, or exits 1 and prints exactly what failed.
"""

import sys
import time

import httpx

BASE = "http://localhost:8000"
FAILURES = []


def check(label: str, cond: bool, extra: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(label)


def wait_for_server(timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{BASE}/api/health", timeout=2).status_code == 200:
                return True
        except httpx.RequestError:
            pass
        time.sleep(0.5)
    return False


def main() -> None:
    if not wait_for_server():
        print("[FAIL] server never came up on :8000")
        sys.exit(1)

    with httpx.Client(base_url=BASE, timeout=10) as client:
        check("health check", client.get("/api/health").status_code == 200)

        email = f"smoke-{int(time.time())}@example.com"
        r = client.post("/api/auth/signup", json={
            "tenant_name": "Smoke Test Co", "email": email, "password": "smoketest123", "name": "Smoke Test",
        })
        check("signup", r.status_code == 200, r.text)
        token = r.json().get("access_token", "")
        headers = {"Authorization": f"Bearer {token}"}

        r = client.post("/api/auth/login", json={"email": email, "password": "smoketest123"})
        check("login with the account just created", r.status_code == 200)

        r = client.get("/api/feature-flags", headers=headers)
        flags = r.json() if r.status_code == 200 else []
        check(
            "feature flags respond with real, well-formed entries",
            r.status_code == 200 and len(flags) >= 4 and all({"key", "label", "description", "enabled"} <= f.keys() for f in flags),
            r.text,
        )

        r = client.post("/api/deals", json={"name": "Smoke Test Deal", "company": "Acme", "value": 1000}, headers=headers)
        check("create a deal", r.status_code == 200, r.text)
        deal_id = r.json().get("id")

        r = client.get("/api/deals", headers=headers)
        check("new deal shows up in the list", r.status_code == 200 and any(d["id"] == deal_id for d in r.json()))

        r = client.get("/api/plugins", headers=headers)
        check("plugin loader discovered at least one plugin", r.status_code == 200 and len(r.json()) >= 1, r.text)

        r = client.get("/metrics")
        check("prometheus metrics endpoint responds", r.status_code == 200 and "slate_http_requests_total" in r.text)

    print("\n" + ("ALL PASSED" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}"))
    sys.exit(0 if not FAILURES else 1)


if __name__ == "__main__":
    main()
