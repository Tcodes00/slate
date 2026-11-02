"""
Shared harness for the verify_*.py scripts (verify_oauth.py predates it and keeps its own copy).

Each script is ONE command that starts everything it needs — a private Redis, the mock upstream,
the real Slate backend on a throwaway SQLite file — each in its own session (setsid), runs real
HTTP checks against it, tears everything down, and exits 0 (ALL PASSED) or 1 (what failed, plus
the tail of the server log). Nothing depends on a shell staying alive between steps.
"""

import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import httpx
import redis

BACKEND = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND))

REDIS_PORT = 6390
SLATE_PORT = 8765
MOCK_PORT = 9300
ADMIN_PASSWORD = "verify-admin-pw-123"

WORKDIR = Path(tempfile.mkdtemp(prefix="slate-verify-"))
DB_PATH = WORKDIR / "slate_verify.db"
FAILURES: list = []
PROCS: list = []
SECTIONS: list = []
CHECKS = 0
_ip_counter = 0


def check(label: str, cond, extra: str = "") -> bool:
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


def spawn(name: str, cmd: list, env: dict = None, cwd=None) -> subprocess.Popen:
    log = open(WORKDIR / f"{name}.log", "wb")
    proc = subprocess.Popen(cmd, cwd=cwd, env={**os.environ, **(env or {})}, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
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
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        try:
            if fn():
                return
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(0.25)
    raise RuntimeError(f"{what} never became ready ({last})")


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_redis(port: int = None) -> subprocess.Popen:
    port = port or REDIS_PORT
    proc = spawn(f"redis-{port}", ["redis-server", "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no", "--dir", str(WORKDIR)])
    wait_until(lambda: redis.Redis(port=port, socket_connect_timeout=1).ping(), 10, f"redis:{port}")
    return proc


def start_mock(port: int = None) -> subprocess.Popen:
    port = port or MOCK_PORT
    proc = spawn(f"mock-{port}", [sys.executable, str(SCRIPTS / "mock_upstream.py")], env={"MOCK_PORT": str(port)})
    wait_until(lambda: httpx.get(f"http://127.0.0.1:{port}/_health", timeout=2).status_code == 200, 30, f"mock:{port}")
    return proc


def start_slate(port: int = None, redis_port: int = None, name: str = "slate", env: dict = None, db_path=None) -> subprocess.Popen:
    port, redis_port = port or SLATE_PORT, redis_port or REDIS_PORT
    full_env = {
        "DATABASE_URL": f"sqlite:///{db_path or DB_PATH}", "REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
        "APP_BASE_URL": f"http://127.0.0.1:{port}", "FRONTEND_URL": "http://127.0.0.1:3999", "ALLOWED_ORIGINS": "http://127.0.0.1:3999",
        **(env or {}),
    }
    proc = spawn(name, [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port), "--proxy-headers",
                        "--forwarded-allow-ips", "127.0.0.1", "--log-level", "warning"], env=full_env, cwd=BACKEND)
    wait_until(lambda: httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2).status_code == 200, 60, name)
    return proc


def fresh_ip() -> str:
    """Each simulated person gets their own client IP (X-Forwarded-For, trusted from loopback), so the per-IP
    rate limits that protect production don't make one check starve the next."""
    global _ip_counter
    _ip_counter += 1
    return f"10.88.{_ip_counter // 250}.{_ip_counter % 250 + 1}"


def api(method: str, path: str, token: str = None, ip: str = None, port: int = None, **kw) -> httpx.Response:
    headers = {"X-Forwarded-For": ip or fresh_ip()}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.request(method, f"http://127.0.0.1:{port or SLATE_PORT}{path}", headers=headers, timeout=kw.pop("timeout", 60), **kw)


def db_all(sql: str, params: tuple = (), db_path=None):
    con = sqlite3.connect(db_path or DB_PATH)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def db_one(sql: str, params: tuple = (), db_path=None):
    rows = db_all(sql, params, db_path)
    return rows[0][0] if rows else None


def db_exec(sql: str, params: tuple = (), db_path=None) -> None:
    con = sqlite3.connect(db_path or DB_PATH)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


class Tenant:
    """A real workspace, created through the real signup endpoint."""

    def __init__(self, name: str, email: str, port: int = None, db_path=None):
        self.name, self.email, self.port, self.db_path = name, email, port, db_path
        r = api("POST", "/api/auth/signup", port=port, json={"tenant_name": name, "email": email, "password": ADMIN_PASSWORD, "name": f"{name} Admin"})
        assert r.status_code == 200, f"signup failed: {r.text}"
        self.token = r.json()["access_token"]
        row = db_all("select id, slug from tenants where name = ?", (name,), db_path)[0]
        self.id, self.slug = row

    def call(self, method: str, path: str, **kw) -> httpx.Response:
        return api(method, path, self.token, port=self.port, **kw)

    def set_flag(self, key: str, on: bool = True) -> None:
        assert self.call("PATCH", f"/api/feature-flags/{key}", json={"enabled": on}).status_code == 200

    def add_user(self, role: str = "standard_user", name: str = None):
        """Invites a teammate and signs them in. Returns (token, email)."""
        email = f"{role}-{int(time.time() * 1000) % 10_000_000}-{self.id}@example.com"
        r = self.call("POST", "/api/auth/invite", json={"name": name or role, "email": email, "password": "teammate-pw-123", "role": role})
        assert r.status_code == 200, r.text
        login = api("POST", "/api/auth/login", port=self.port, json={"email": email, "password": "teammate-pw-123"})
        return login.json()["access_token"], email


def run(title: str, setup, log_names=("slate",)) -> int:
    if shutil.which("redis-server") is None:
        print("[FAIL] redis-server isn't on PATH — these checks need a real Redis (apt install redis-server / brew install redis)")
        return 2
    started = time.time()
    print(f"== {title} ==\nworkdir: {WORKDIR}")
    try:
        setup()
        for name, fn in SECTIONS:
            print(f"\n-- {name} --")
            try:
                fn()
            except Exception:  # noqa: BLE001 — a crashed section is a failure and must not hide the ones after it
                traceback.print_exc()
                check(f"section crashed: {name}", False)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        check("setup", False)
    finally:
        for _name, proc in reversed(PROCS):
            kill(proc)
        for _name, proc in PROCS:
            if proc.poll() is None:
                kill(proc, signal.SIGKILL)
    print(f"\n{CHECKS} checks in {time.time() - started:.0f}s")
    if FAILURES:
        for name in log_names:
            log = WORKDIR / f"{name}.log"
            if log.exists():
                print(f"\n--- tail of {name}.log ---\n" + "\n".join(log.read_text(errors="replace").splitlines()[-25:]))
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}\n(logs kept in {WORKDIR})")
        return 1
    shutil.rmtree(WORKDIR, ignore_errors=True)
    print("ALL PASSED")
    return 0
