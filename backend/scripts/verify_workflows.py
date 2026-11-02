#!/usr/bin/env python3
"""
End-to-end verification of the workflow engine (backend/workflow_engine.py, routers/workflows.py).

    python backend/scripts/verify_workflows.py

Starts a private Redis, the mock upstream (the HTTP service workflows call), and real backends — one
configured for fast schedules/tight limits, a second replica on the same database (to prove a schedule
fires once, not once per replica), and one in production mode (SSRF guard) — then drives workflows
over the real API and inspects what actually happened: what the upstream received, what each node
recorded, what the database holds. See verify_common.py for the harness contract.
"""

import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timedelta

import httpx
import redis
from cryptography.fernet import Fernet

import verify_common as v
from verify_common import MOCK_PORT, REDIS_PORT, SLATE_PORT, WORKDIR, Tenant, api, check, db_all, db_exec, db_one, section

PROD_PORT, REPLICA_PORT = 8767, 8768
PROD_DB = WORKDIR / "prod.db"
MOCK = f"http://127.0.0.1:{MOCK_PORT}"
SERVER_ENV = {
    "WORKFLOW_MIN_INTERVAL": "1", "WORKFLOW_MAX_CONCURRENT": "3", "WORKFLOW_MAX_RUN_SECONDS": "8", "WORKFLOW_SCHEDULER_TICK": "0.5",
    "WORKFLOW_HEARTBEAT": "0.5", "WORKFLOW_STALE_AFTER": "3", "LOCAL_LLM_BASE_URL": f"{MOCK}/v1", "LOCAL_LLM_MODEL": "mock-local",
}
TA = TB = TF = TP = None
CRED_SECRET = "tok-SECRET-abc-123"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{v.DB_PATH}")


# ---------------------------------------------------------------- helpers
def N(node_id, node_type, params=None, **extra):
    return {"id": node_id, "type": node_type, "params": params or {}, **extra}


def E(src, dst, branch=None):
    return {"from": src, "to": dst, **({"branch": branch} if branch else {})}


TRIGGER = N("trigger", "trigger", {"kind": "manual"})


def definition(nodes, edges):
    return {"nodes": nodes, "edges": edges}


def create(tenant, name, nodes, edges, expect=200):
    r = tenant.call("POST", "/api/workflows", json={"name": name, "definition": definition(nodes, edges)})
    assert r.status_code == expect, f"create '{name}' → {r.status_code} {r.text}"
    return r.json()


def run(tenant, wf, payload=None):
    r = tenant.call("POST", f"/api/workflows/{wf['id']}/run", json={"input": payload or {}, "wait": True}, timeout=60)
    assert r.status_code == 200, f"run '{wf['name']}' → {r.status_code} {r.text}"
    return r.json()


def node(run_detail, node_id):
    return next((n for n in run_detail["nodes"] if n["node_id"] == node_id), None)


def seconds(run_detail):
    return (datetime.fromisoformat(run_detail["finished_at"]) - datetime.fromisoformat(run_detail["started_at"])).total_seconds()


def mock_reset():
    httpx.post(f"{MOCK}/_reset", timeout=5)


def mock_log(prefix):
    return [e for e in httpx.get(f"{MOCK}/_log", timeout=5).json() if e["path"].startswith(prefix)]


def runs_of(tenant, wf, **params):
    return tenant.call("GET", f"/api/workflows/{wf['id']}/runs", params={"limit": 200, **params}).json()


def wait_for(fn, seconds_=10.0, step=0.2):
    deadline = time.time() + seconds_
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(step)
    return fn()


def sign(secret, body: bytes, ts=None):
    ts = str(int(time.time()) if ts is None else ts)
    return {"X-Slate-Timestamp": ts, "X-Slate-Signature": "v1=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest(), "Content-Type": "application/json"}


def hook(wf, secret, payload=None, *, raw=None, headers=None, wait=False):
    body = raw if raw is not None else json.dumps(payload or {}).encode()
    return httpx.post(wf["webhook_url"], content=body, headers={**(headers if headers is not None else sign(secret, body)), "X-Forwarded-For": v.fresh_ip()},
                      params={"wait": "true"} if wait else None, timeout=60)


def http_node(node_id, path, query=None, **extra):
    return N(node_id, "http_request", {"method": "GET", "url": f"{MOCK}{path}", "query": query or {}}, **extra)


# ===========================================================================
@section("1. Gating, roles, and definition validation")
def sec_validation():
    check("flag OFF (the default): every workflow endpoint is refused (403) with a message saying how to turn it on",
          TF.call("GET", "/api/workflows").status_code == 403 and "turned off" in TF.call("GET", "/api/workflows").text)
    standard, _ = TA.add_user("standard_user")
    manager, _ = TA.add_user("manager")
    check("standard_user: refused on list, create and credentials (workflows can call out and send mail — manager and up)",
          all(api("GET", "/api/workflows", standard).status_code == 403 for _ in (0,)) and api("POST", "/api/workflows", standard, json={"name": "x", "definition": {}}).status_code == 403
          and api("GET", "/api/workflow-credentials", standard).status_code == 403)
    check("manager: allowed", api("GET", "/api/workflows", manager).status_code == 200)
    check("anonymous: 401", api("GET", "/api/workflows").status_code == 401)

    def errors_for(nodes, edges):
        r = TA.call("POST", "/api/workflows/validate", json={"definition": definition(nodes, edges)})
        return r.json()["errors"] if r.status_code == 200 else [r.text]

    ok_nodes, ok_edges = [TRIGGER, N("a", "set", {"values": {"x": 1}})], [E("trigger", "a")]
    r = TA.call("POST", "/api/workflows/validate", json={"definition": definition(ok_nodes, ok_edges)}).json()
    check("a valid definition validates", r["valid"] is True and r["errors"] == [])

    cases = [
        ("no trigger", [N("a", "set", {"values": {}})], [], "exactly one trigger"),
        ("two triggers", [TRIGGER, N("t2", "trigger", {"kind": "manual"})], [], "exactly one trigger"),
        ("a cycle", [TRIGGER, N("a", "set", {"values": {}}), N("b", "set", {"values": {}})], [E("trigger", "a"), E("a", "b"), E("b", "a")], "cycle"),
        ("unknown node type", [TRIGGER, N("a", "run_python", {})], [E("trigger", "a")], "unknown type"),
        ("duplicate ids", [TRIGGER, N("a", "set", {"values": {}}), N("a", "set", {"values": {}})], [E("trigger", "a")], "Duplicate node id"),
        ("an edge to nowhere", ok_nodes, [E("trigger", "a"), E("a", "ghost")], "must connect two existing nodes"),
        ("a self-loop", ok_nodes, [E("trigger", "a"), E("a", "a")], "connect to itself"),
        ("an unreachable node", [TRIGGER, N("a", "set", {"values": {}}), N("orphan", "set", {"values": {}})], [E("trigger", "a")], "isn't connected"),
        ("an edge into the trigger", ok_nodes, [E("trigger", "a"), E("a", "trigger")], "incoming edges"),
        ("a branch label on a node that has no branches", ok_nodes, [E("trigger", "a", "true")], "has no branches"),
        ("a condition with an unknown operator", [TRIGGER, N("c", "condition", {"left": 1, "op": "~="})], [E("trigger", "c")], "'op' from"),
        ("a condition edge without a valid branch", [TRIGGER, N("c", "condition", {"left": 1, "op": "=="}), N("a", "set", {"values": {}})], [E("trigger", "c"), E("c", "a", "maybe")], "isn't valid"),
        ("a switch case labelled 'default'", [TRIGGER, N("s", "switch", {"value": 1, "cases": [{"label": "default", "equals": 1}]})], [E("trigger", "s")], "not 'default'"),
        ("an http node without a url", [TRIGGER, N("h", "http_request", {"method": "GET"})], [E("trigger", "h")], "needs 'url'"),
        ("an http node with a bad method", [TRIGGER, N("h", "http_request", {"url": "http://x", "method": "TRACE"})], [E("trigger", "h")], "method must be"),
        ("a trigger of unknown kind", [N("trigger", "trigger", {"kind": "telepathy"})], [], "Trigger kind"),
        ("a schedule with neither interval nor cron", [N("trigger", "trigger", {"kind": "schedule"})], [], "exactly one of"),
        ("a schedule below the minimum interval", [N("trigger", "trigger", {"kind": "schedule", "interval_seconds": 0})], [], "at least 1"),
        ("an invalid cron expression", [N("trigger", "trigger", {"kind": "schedule", "cron": "not a cron"})], [], "valid cron"),
        ("an event trigger without an event type", [N("trigger", "trigger", {"kind": "event"})], [], "event_type"),
        ("a delay over 30 seconds", [TRIGGER, N("d", "delay", {"seconds": 31})], [E("trigger", "d")], "between 0 and 30"),
        ("an out-of-range retry", [TRIGGER, N("a", "set", {"values": {}}, retry={"max_attempts": 9})], [E("trigger", "a")], "max_attempts 1-5"),
        ("an unknown on_error policy", [TRIGGER, N("a", "set", {"values": {}}, on_error="explode")], [E("trigger", "a")], "on_error"),
        ("a node named 'run' (reserved for the run's own data)", [TRIGGER, N("run", "set", {"values": {}})], [E("trigger", "run")], "reserved"),
        ("an 'error' branch from a node that hasn't opted in with on_error=branch", ok_nodes + [N("b", "set", {"values": {}})], [E("trigger", "a"), E("a", "b", "error")], "isn't valid"),
        ("an unsafe create_record entity", [TRIGGER, N("r", "create_record", {"entity": "user", "fields": {}})], [E("trigger", "r")], "'deal' or 'ticket'"),
        ("no nodes", [], [], "non-empty"),
        ("more than 50 nodes", [TRIGGER] + [N(f"n{i}", "set", {"values": {}}) for i in range(60)], [], "at most 50"),
    ]
    for label, nodes, edges, expect in cases:
        found = errors_for(nodes, edges)
        check(f"rejected: {label}", any(expect in e for e in found), str(found))
    check("the node catalog the editor needs is served, including every node type and the limits",
          {"trigger", "http_request", "condition", "switch", "ai_prompt", "create_record", "send_email", "merge", "delay", "fail"} <= set(TA.call("GET", "/api/workflows/node-types").json()["nodes"]))

    wf = create(TA, "validation-demo", ok_nodes, ok_edges)
    check("creating saves it DISABLED, version 1, and shows the webhook secret exactly once", wf["enabled"] is False and wf["version"] == 1 and bool(wf["webhook_secret"]))
    check("…later reads never include the secret", "webhook_secret" not in TA.call("GET", f"/api/workflows/{wf['id']}").json() and wf["webhook_secret"] not in TA.call("GET", "/api/workflows").text)
    check("duplicate name → 409; blank name → 400",
          TA.call("POST", "/api/workflows", json={"name": "validation-demo", "definition": definition(ok_nodes, ok_edges)}).status_code == 409
          and TA.call("POST", "/api/workflows", json={"name": " ", "definition": definition(ok_nodes, ok_edges)}).status_code == 400)
    check("an invalid definition is refused on create with the reasons", "cycle" in TA.call("POST", "/api/workflows", json={"name": "bad", "definition": definition([TRIGGER, N("a", "set", {"values": {}}), N("b", "set", {"values": {}})], [E("trigger", "a"), E("a", "b"), E("b", "a")])}).text)
    big = definition([TRIGGER, N("a", "set", {"values": {"blob": "x" * 250_000}})], [E("trigger", "a")])
    check("an oversized definition (>200 KB) → 400", TA.call("POST", "/api/workflows", json={"name": "big", "definition": big}).status_code == 400)
    r = TA.call("PUT", f"/api/workflows/{wf['id']}", json={"definition": definition(ok_nodes + [N("b", "set", {"values": {"y": 2}})], ok_edges + [E("a", "b")])})
    check("editing bumps the version", r.status_code == 200 and r.json()["version"] == 2)


# ===========================================================================
@section("2. A real run: typed templating, HTTP, per-node records, immutable history")
def sec_linear():
    nodes = [
        TRIGGER,
        N("s1", "set", {"values": {
            "greeting": "Hello {{ trigger.name | upper }}!", "n": "{{ trigger.n }}", "missing": "{{ trigger.nope }}",
            "dflt": "{{ trigger.nope | default(\"fallback\") }}", "inj": "{{ __import__('os').system('id') }}", "dunder": "{{ trigger.__class__ }}",
            "len": "{{ trigger.items | length }}", "first": "{{ trigger.items[0].id }}", "literal": "{{ trigger.payload }}",
        }}),
        N("fetch", "http_request", {"method": "POST", "url": f"{MOCK}/echo", "query": {"q": "{{ s1.greeting }}"}, "headers": {"X-Run": "{{ run.id }}"},
                                    "body": {"count": "{{ s1.n }}", "who": "{{ trigger.name }}"}}),
        N("s2", "set", {"values": {"echoed_count": "{{ fetch.body.body.count }}", "status": "{{ fetch.status }}", "shout": "{{ fetch.body.query.q | upper }}"}}),
    ]
    wf = create(TA, "linear", nodes, [E("trigger", "s1"), E("s1", "fetch"), E("fetch", "s2")])
    mock_reset()
    r = run(TA, wf, {"name": "ada", "n": 3, "items": [{"id": 1}, {"id": 2}], "payload": "{{ trigger.secret }}", "secret": "do-not-leak"})
    s1 = node(r, "s1")["output"]
    check("the run succeeded, every node once", r["status"] == "succeeded" and [n["status"] for n in r["nodes"]] == ["succeeded"] * 4 and all(n["attempts"] == 1 for n in r["nodes"]), json.dumps(r)[:400])
    check("templates: string interpolation + filter ('Hello ADA!')", s1["greeting"] == "Hello ADA!")
    check("templates: a lone {{ expr }} keeps its TYPE (3 stays the number 3, a count is an int)", s1["n"] == 3 and isinstance(s1["n"], int) and s1["len"] == 2 and s1["first"] == 1)
    check("templates: missing paths are null, and `default(...)` fills them", s1["missing"] is None and s1["dflt"] == "fallback")
    check("templates: code is NEVER evaluated — `__import__('os').system('id')` and `trigger.__class__` just resolve to nothing", s1["inj"] is None and s1["dunder"] is None)
    check("templates: data is data — a trigger value that itself contains '{{ … }}' is NOT evaluated a second time", s1["literal"] == "{{ trigger.secret }}")

    seen = mock_log("/echo")[-1]
    check("the HTTP node really sent what the templates said (query, header, and a typed JSON body)",
          seen["method"] == "POST" and seen["query"]["q"] == "Hello ADA!" and seen["headers"]["x-run"] == str(r["id"]) and seen["body"] == {"count": 3, "who": "ada"}, str(seen))
    check("the next node read the HTTP response (typed): echoed_count 3, status 200, uppercase of the echoed query",
          node(r, "s2")["output"] == {"echoed_count": 3, "status": 200, "shout": "HELLO ADA!"}, str(node(r, "s2")["output"]))
    check("the run's output is its leaf node's output, and every node recorded duration and rendered input",
          r["output"] == {"s2": node(r, "s2")["output"]} and all(n["duration_ms"] is not None for n in r["nodes"]) and node(r, "fetch")["input"]["url"] == f"{MOCK}/echo")
    check("trigger data is recorded on the run", r["trigger_data"]["name"] == "ada" and r["trigger_type"] == "manual")

    before = db_one("select definition from workflow_runs where id = ?", (r["id"],))
    TA.call("PUT", f"/api/workflows/{wf['id']}", json={"definition": definition([TRIGGER, N("only", "set", {"values": {"changed": True}})], [E("trigger", "only")])})
    check("editing the workflow afterwards doesn't rewrite history: the old run keeps the exact definition it ran",
          db_one("select definition from workflow_runs where id = ?", (r["id"],)) == before and TA.call("GET", f"/api/workflows/runs/{r['id']}").json()["workflow_version"] == 1)


# ===========================================================================
@section("3. Credential vault: secrets never appear anywhere, and can't be aimed at the wrong host")
def sec_credentials():
    bad = {"name": "c", "kind": "bearer", "secret": "s"}
    check("a credential must be bound to hosts (else anyone who can build workflows could read the secret off their own server) → 400",
          TA.call("POST", "/api/workflow-credentials", json={**bad, "allowed_hosts": []}).status_code == 400
          and TA.call("POST", "/api/workflow-credentials", json={**bad, "allowed_hosts": ["*"]}).status_code == 400
          and TA.call("POST", "/api/workflow-credentials", json={**bad, "kind": "carrier-pigeon", "allowed_hosts": ["x.com"]}).status_code == 400
          and TA.call("POST", "/api/workflow-credentials", json={**bad, "kind": "header", "allowed_hosts": ["x.com"]}).status_code == 400)
    r = TA.call("POST", "/api/workflow-credentials", json={"name": "mock-bearer", "kind": "bearer", "secret": CRED_SECRET, "allowed_hosts": ["127.0.0.1"]})
    check("a bound credential saves, and the response has no secret", r.status_code == 200 and CRED_SECRET not in r.text and r.json()["allowed_hosts"] == ["127.0.0.1"], r.text)
    cred_id = r.json()["id"]
    raw = db_one("select secret_encrypted from workflow_credentials where id = ?", (cred_id,))
    import security
    check("raw SQLite read: Fernet ciphertext, and it decrypts back to the secret", bool(raw) and CRED_SECRET not in raw and raw.startswith("gAAAA") and security.decrypt_field(raw) == CRED_SECRET)
    check("duplicate name → 409; the list never shows secrets", TA.call("POST", "/api/workflow-credentials", json={"name": "mock-bearer", "kind": "bearer", "secret": "z", "allowed_hosts": ["x.com"]}).status_code == 409
          and CRED_SECRET not in TA.call("GET", "/api/workflow-credentials").text)

    wf = create(TA, "with-credential", [TRIGGER, N("call", "http_request", {"method": "GET", "url": f"{MOCK}/echo", "credential": "mock-bearer"})], [E("trigger", "call")])
    mock_reset()
    r = run(TA, wf)
    seen = mock_log("/echo")
    check("the upstream received 'Authorization: Bearer <secret>' — the credential was injected at the last moment", r["status"] == "succeeded" and seen and seen[-1]["headers"].get("authorization") == f"Bearer {CRED_SECRET}")
    stored = json.dumps(r) + json.dumps(TA.call("GET", f"/api/workflows/{wf['id']}").json()) + "".join(str(x) for x in db_all("select input, output from workflow_node_runs"))
    check("…but the secret is in NO run record, API response, or stored node input/output (the echoed header is redacted by key name)",
          CRED_SECRET not in stored and "[redacted]" in json.dumps(node(r, "call")["output"]))
    check("…nor in the audit log", CRED_SECRET not in json.dumps(TA.call("GET", "/api/audit-log").json()))

    wf2 = create(TA, "wrong-host", [TRIGGER, N("call", "http_request", {"method": "GET", "url": f"http://localhost:{MOCK_PORT}/echo", "credential": "mock-bearer"})], [E("trigger", "call")])
    mock_reset()
    r = run(TA, wf2)
    check("pointing the credential at a host it isn't bound to (localhost vs 127.0.0.1) fails the node, and NOTHING is sent",
          r["status"] == "failed" and "isn't allowed to be sent to localhost" in r["error"] and mock_log("/echo") == [], r["error"] or "")
    wf3 = create(TA, "no-such-cred", [TRIGGER, N("call", "http_request", {"method": "GET", "url": f"{MOCK}/echo", "credential": "ghost"})], [E("trigger", "call")])
    check("enabling a workflow that names a credential that doesn't exist → 400", TA.call("PATCH", f"/api/workflows/{wf3['id']}/enabled", json={"enabled": True}).status_code == 400)
    check("deleting a credential that a workflow uses → 409", TA.call("DELETE", f"/api/workflow-credentials/{cred_id}").status_code == 409)
    TA.call("DELETE", f"/api/workflows/{wf['id']}")
    TA.call("DELETE", f"/api/workflows/{wf2['id']}")
    check("…and it can be deleted once nothing uses it", TA.call("DELETE", f"/api/workflow-credentials/{cred_id}").status_code == 200)


# ===========================================================================
@section("4. Branching, switch, merge — and the branch not taken is skipped, not run")
def sec_branching():
    nodes = [TRIGGER, N("cond", "condition", {"left": "{{ trigger.score }}", "op": ">=", "right": 50}),
             N("hi", "set", {"values": {"tier": "gold"}}), N("lo", "set", {"values": {"tier": "basic"}}), N("join", "merge"),
             N("final", "set", {"values": {"hi": "{{ hi.tier }}", "lo": "{{ lo.tier }}", "joined": "{{ join.inputs | json }}"}})]
    wf = create(TA, "branch", nodes, [E("trigger", "cond"), E("cond", "hi", "true"), E("cond", "lo", "false"), E("hi", "join"), E("lo", "join"), E("join", "final")])
    for score, took, skipped in ((80, "hi", "lo"), (10, "lo", "hi"), (None, "lo", "hi")):
        r = run(TA, wf, {} if score is None else {"score": score})
        out = node(r, "final")["output"]
        check(f"condition (score={score}): only the '{took}' branch ran; '{skipped}' is recorded as skipped with a reason",
              r["status"] == "succeeded" and node(r, took)["status"] == "succeeded" and node(r, skipped)["status"] == "skipped"
              and "no branch" in node(r, skipped)["error"] and node(r, skipped)["attempts"] == 0, str([(n["node_id"], n["status"]) for n in r["nodes"]]))
        check(f"…and the merge waited for, and joined, only the branch that ran (inputs: {took})",
              json.loads(out["joined"]).keys() == {took} and (out[took] is not None) and out[skipped] is None, str(out))

    sw = create(TA, "switch", [TRIGGER, N("sw", "switch", {"value": "{{ trigger.plan }}", "cases": [{"label": "free", "equals": "free"}, {"label": "pro", "equals": "pro"}]}),
                               N("a", "set", {"values": {"p": "A"}}), N("b", "set", {"values": {"p": "B"}}), N("c", "set", {"values": {"p": "C"}})],
                [E("trigger", "sw"), E("sw", "a", "free"), E("sw", "b", "pro"), E("sw", "c", "default")])
    for plan, ran in (("pro", "b"), ("free", "a"), ("enterprise", "c")):
        r = run(TA, sw, {"plan": plan})
        check(f"switch (plan={plan}): exactly one branch ({ran}) ran", [n["node_id"] for n in r["nodes"] if n["status"] == "succeeded" and len(n["node_id"]) == 1] == [ran]
              and sum(n["status"] == "skipped" for n in r["nodes"]) == 2)

    multi = create(TA, "multi-cond", [TRIGGER, N("c", "condition", {"conditions": [{"left": "{{ trigger.a }}", "op": "==", "right": 1}, {"left": "{{ trigger.b }}", "op": "contains", "right": "x"}], "mode": "or"}),
                                      N("yes", "set", {"values": {"y": 1}}), N("no", "set", {"values": {"n": 1}})], [E("trigger", "c"), E("c", "yes", "true"), E("c", "no", "false")])
    check("multiple conditions combine with 'or'", node(run(TA, multi, {"a": 2, "b": "xyz"}), "yes")["status"] == "succeeded" and node(run(TA, multi, {"a": 2, "b": "abc"}), "no")["status"] == "succeeded")

    import workflow_engine as eng
    c = eng.eval_condition
    truth = [(c("3", "==", 3), True), (c("abc", "!=", "abd"), True), (c("10", ">", 9), True), (c("abc", ">", 1), False), (c([1, 2], "contains", 2), True),
             (c("hello", "contains", "ell"), True), (c({"k": 1}, "contains", "k"), True), (c("hello", "not_contains", "z"), True), (c("hello", "starts_with", "he"), True),
             (c("hello", "ends_with", "lo"), True), (c("a", "in", ["a", "b"]), True), (c("z", "in", "abc"), False), (c(None, "exists", None), False), (c("x", "exists", None), True),
             (c("", "not_exists", None), True), (c(None, "==", None), True)]
    check("every comparison operator behaves (numeric coercion, contains on list/dict/str, in, exists…)", all(got is want for got, want in truth), str([i for i, (g, w) in enumerate(truth) if g is not w]))


# ===========================================================================
@section("5. Independent branches run in parallel")
def sec_parallel():
    nodes = [TRIGGER] + [N(f"d{i}", "delay", {"seconds": 1.2}) for i in (1, 2, 3)] + [N("join", "merge")]
    wf = create(TA, "parallel", nodes, [E("trigger", f"d{i}") for i in (1, 2, 3)] + [E(f"d{i}", "join") for i in (1, 2, 3)])
    r = run(TA, wf)
    starts = [datetime.fromisoformat(node(r, f"d{i}")["started_at"]) for i in (1, 2, 3)]
    check("three 1.2-second delays finish in about 1.2s, not 3.6s — they ran concurrently", r["status"] == "succeeded" and seconds(r) < 2.6, f"{seconds(r):.2f}s")
    check("their start times are within 0.3s of each other", (max(starts) - min(starts)).total_seconds() < 0.3)
    check("the merge started only after the slowest branch finished, and ran once",
          sum(n["node_id"] == "join" for n in r["nodes"]) == 1 and datetime.fromisoformat(node(r, "join")["started_at"]) >= max(datetime.fromisoformat(node(r, f"d{i}")["started_at"]) + timedelta(seconds=1.1) for i in (1, 2, 3)))


# ===========================================================================
@section("6. Retries, timeouts, and the three error policies")
def sec_errors():
    key = secrets.token_hex(4)
    wf = create(TA, "retry-ok", [TRIGGER, http_node("f", "/flaky", {"key": key, "fail": 2}, retry={"max_attempts": 3, "backoff_seconds": 0})], [E("trigger", "f")])
    mock_reset()
    r = run(TA, wf)
    check("a flaky upstream (503, 503, then 200) succeeds on attempt 3, recorded as 3 attempts, and the upstream really saw 3 requests",
          r["status"] == "succeeded" and node(r, "f")["attempts"] == 3 and len(mock_log("/flaky")) == 3, str(node(r, "f")))

    key = secrets.token_hex(4)
    wf = create(TA, "retry-exhausted", [TRIGGER, http_node("f", "/flaky", {"key": key, "fail": 5}, retry={"max_attempts": 2, "backoff_seconds": 0}), N("after", "set", {"values": {"x": 1}})], [E("trigger", "f"), E("f", "after")])
    mock_reset()
    r = run(TA, wf)
    check("retries exhausted → the run FAILS with the node and the reason, after exactly 2 attempts",
          r["status"] == "failed" and "Node 'f' failed: HTTP 503" in r["error"] and node(r, "f")["attempts"] == 2 and len(mock_log("/flaky")) == 2, r["error"] or "")
    check("…and everything downstream is skipped with the reason", node(r, "after")["status"] == "skipped" and "earlier step failed" in node(r, "after")["error"])

    wf = create(TA, "no-retry-4xx", [TRIGGER, http_node("f", "/status/404", retry={"max_attempts": 3, "backoff_seconds": 0})], [E("trigger", "f")])
    mock_reset()
    r = run(TA, wf)
    check("a 404 is NOT retried (it'd fail identically every time): 1 attempt, 1 request", r["status"] == "failed" and node(r, "f")["attempts"] == 1 and len(mock_log("/status")) == 1)
    wf = create(TA, "retry-429", [TRIGGER, http_node("f", "/status/429", retry={"max_attempts": 2, "backoff_seconds": 0})], [E("trigger", "f")])
    mock_reset()
    r = run(TA, wf)
    check("a 429 IS retried", node(r, "f")["attempts"] == 2 and len(mock_log("/status")) == 2)
    wf = create(TA, "expect-404", [TRIGGER, N("f", "http_request", {"method": "GET", "url": f"{MOCK}/status/404", "expect_status": [404]})], [E("trigger", "f")])
    r = run(TA, wf)
    check("expect_status [404] makes a 404 a success (APIs that use it meaningfully)", r["status"] == "succeeded" and node(r, "f")["output"]["status"] == 404)

    wf = create(TA, "node-timeout", [TRIGGER, http_node("f", "/slow", {"seconds": 3}, timeout_seconds=1)], [E("trigger", "f")])
    t0 = time.time()
    r = run(TA, wf)
    check("a node that exceeds its timeout fails with 'Timed out after 1s' — promptly, not after the upstream finishes", r["status"] == "failed" and "Timed out after 1s" in r["error"] and time.time() - t0 < 2.9, f"{time.time() - t0:.1f}s")

    wf = create(TA, "continue", [TRIGGER, http_node("bad", "/status/500", on_error="continue"), N("after", "set", {"values": {"msg": "{{ bad.error }}", "status": "{{ bad.status }}"}})], [E("trigger", "bad"), E("bad", "after")])
    r = run(TA, wf)
    check("on_error=continue: the node is recorded FAILED, the run SUCCEEDS, and the next node can read the error",
          r["status"] == "succeeded" and node(r, "bad")["status"] == "failed" and "HTTP 500" in node(r, "after")["output"]["msg"] and node(r, "after")["output"]["status"] == 500, str(node(r, "after")))

    wf = create(TA, "error-branch", [TRIGGER, http_node("bad", "/status/500", on_error="branch"), N("handler", "set", {"values": {"handled": "{{ bad.error }}"}}), N("normal", "set", {"values": {"ok": 1}})],
                [E("trigger", "bad"), E("bad", "handler", "error"), E("bad", "normal")])
    r = run(TA, wf)
    check("on_error=branch: the 'error' edge runs the handler, the normal edge is skipped, and the run succeeds",
          r["status"] == "succeeded" and node(r, "handler")["status"] == "succeeded" and node(r, "normal")["status"] == "skipped" and "HTTP 500" in node(r, "handler")["output"]["handled"])
    wf = create(TA, "error-branch-ok", [TRIGGER, http_node("good", "/status/200", on_error="branch"), N("handler", "set", {"values": {"h": 1}}), N("normal", "set", {"values": {"ok": 1}})],
                [E("trigger", "good"), E("good", "handler", "error"), E("good", "normal")])
    r = run(TA, wf)
    check("…and when the node succeeds, it's the other way round", node(r, "normal")["status"] == "succeeded" and node(r, "handler")["status"] == "skipped")

    wf = create(TA, "fail-node", [TRIGGER, N("boom", "fail", {"message": "boom {{ trigger.x }}"}), N("slow", "delay", {"seconds": 1}), N("after", "set", {"values": {"a": 1}})], [E("trigger", "boom"), E("trigger", "slow"), E("boom", "after")])
    r = run(TA, wf, {"x": 7})
    check("a fail node stops the run with its (templated) message, lets the in-flight sibling finish cleanly, and skips what follows",
          r["status"] == "failed" and r["error"] == "Node 'boom' failed: boom 7" and node(r, "slow")["status"] == "succeeded" and node(r, "after")["status"] == "skipped", str(r["error"]))

    wf = create(TA, "big-text", [TRIGGER, http_node("f", "/big", {"kb": 100})], [E("trigger", "f")])
    check("a large text response is capped at 20,000 characters in the node's output", len(node(run(TA, wf), "f")["output"]["body"]) == 20_000)
    wf = create(TA, "too-big", [TRIGGER, http_node("f", "/big", {"kb": 1500})], [E("trigger", "f")])
    r = run(TA, wf)
    check("a response over 1 MB fails the node (cut off while streaming, not buffered)", r["status"] == "failed" and "larger than" in r["error"], r["error"] or "")


# ===========================================================================
@section("7. Triggers: signed webhooks, schedules (and two replicas), and events")
def sec_triggers():
    wf = create(TA, "hooked", [N("trigger", "trigger", {"kind": "webhook"}), N("out", "set", {"values": {"who": "{{ trigger.name }}", "n": "{{ trigger.n }}"}})], [E("trigger", "out")])
    secret = wf["webhook_secret"]
    check("a webhook workflow exposes its public URL (an unguessable id), only after it's saved", wf["webhook_url"].startswith(f"http://127.0.0.1:{SLATE_PORT}/api/hooks/") and len(wf["webhook_url"].rsplit("/", 1)[1]) >= 20)
    check("while DISABLED the URL is a plain 404 (it doesn't even admit the id exists)", hook(wf, secret, {"name": "x"}).status_code == 404)
    TA.call("PATCH", f"/api/workflows/{wf['id']}/enabled", json={"enabled": True})

    r = hook(wf, secret, {"name": "ada", "n": 5})
    check("a correctly signed delivery → 202 Accepted with a run id", r.status_code == 202 and r.json()["status"] == "queued", f"{r.status_code} {r.text}")
    run_id = r.json()["run_id"]
    done = wait_for(lambda: (lambda d: d if d["status"] in ("succeeded", "failed") else None)(TA.call("GET", f"/api/workflows/runs/{run_id}").json()))
    check("…and the payload reached the workflow as trigger data", done["status"] == "succeeded" and done["trigger_type"] == "webhook" and done["output"]["out"] == {"who": "ada", "n": 5}, str(done))
    r = hook(wf, secret, {"name": "bob", "n": 1}, wait=True)
    check("?wait=true returns the finished run's output synchronously", r.status_code == 200 and r.json()["status"] == "succeeded" and r.json()["output"]["out"]["who"] == "bob", r.text)

    body = json.dumps({"name": "x"}).encode()
    for label, kwargs, want in (("no signature headers", {"headers": {"Content-Type": "application/json"}}, 401), ("a signature made with the wrong secret", {"headers": sign("wrong-secret", body)}, 401),
                                ("a valid signature for a DIFFERENT body (tampered in transit)", {"headers": sign(secret, b'{"name": "y"}')}, 401),
                                ("a stale timestamp (10 minutes old) — replay protection", {"headers": sign(secret, body, ts=int(time.time()) - 600)}, 401),
                                ("a timestamp from the future", {"headers": sign(secret, body, ts=int(time.time()) + 600)}, 401),
                                ("a garbled signature header", {"headers": {**sign(secret, body), "X-Slate-Signature": "v1=zzz"}}, 401)):
        check(f"signature check: {label} → {want}", hook(wf, secret, raw=body, **kwargs).status_code == want)
    check("a signed body that isn't valid JSON → 400; one that is a JSON array → 400",
          hook(wf, secret, raw=b"not json").status_code == 400 and hook(wf, secret, raw=b"[1,2]").status_code == 400)
    check("a signed body over 1 MB → 413", hook(wf, secret, raw=b'{"a": "' + b"x" * 1_100_000 + b'"}').status_code == 413)
    check("an unknown webhook id → 404", httpx.post(f"http://127.0.0.1:{SLATE_PORT}/api/hooks/{secrets.token_urlsafe(18)}", content=b"{}", timeout=10).status_code == 404)
    check("none of the rejected deliveries created a run", len([x for x in runs_of(TA, wf)]) == 2)

    rot = TA.call("POST", f"/api/workflows/{wf['id']}/rotate-webhook-secret").json()
    check("rotating the secret: the old one stops working immediately, the new one works",
          hook(wf, secret, {"name": "z"}).status_code == 401 and hook(wf, rot["webhook_secret"], {"name": "z"}).status_code == 202)
    check("workspace B can't see, run, or rotate it (404)", all(TB.call(m, p, **kw).status_code == 404 for m, p, kw in (
        ("GET", f"/api/workflows/{wf['id']}", {}), ("POST", f"/api/workflows/{wf['id']}/run", {"json": {}}), ("POST", f"/api/workflows/{wf['id']}/rotate-webhook-secret", {}))))
    TA.set_flag("workflow_engine", False)
    check("the workspace's flag turned OFF → the webhook goes dark (404) even though the workflow is enabled", hook(wf, rot["webhook_secret"], {"name": "z"}).status_code == 404)
    TA.set_flag("workflow_engine", True)

    nosig = create(TA, "unsigned", [N("trigger", "trigger", {"kind": "webhook", "require_signature": False}), N("out", "set", {"values": {"v": "{{ trigger.v }}"}})], [E("trigger", "out")])
    TA.call("PATCH", f"/api/workflows/{nosig['id']}/enabled", json={"enabled": True})
    check("require_signature=false (for senders that can't sign) accepts an unsigned POST — the URL id alone is then the secret",
          hook(nosig, "", {"v": 1}, headers={"Content-Type": "application/json"}).status_code == 202)

    # ---- concurrency cap
    slow = create(TA, "slow-hook", [N("trigger", "trigger", {"kind": "webhook"}), N("d", "delay", {"seconds": 3})], [E("trigger", "d")])
    TA.call("PATCH", f"/api/workflows/{slow['id']}/enabled", json={"enabled": True})
    time.sleep(1)
    codes = [hook(slow, slow["webhook_secret"], {"i": i}).status_code for i in range(6)]
    check("CONCURRENCY CAP: with 3 runs allowed in flight per workspace, a burst of 6 → 3 accepted, 3 refused with 429", codes.count(202) == 3 and codes.count(429) == 3, str(codes))
    wait_for(lambda: all(r["status"] in ("succeeded", "failed") for r in runs_of(TA, slow)), 12)
    check("…and once they finish, new runs are accepted again", hook(slow, slow["webhook_secret"], {"i": 9}).status_code == 202)
    wait_for(lambda: all(r["status"] in ("succeeded", "failed") for r in runs_of(TA, slow)), 12)

    # ---- schedule
    sched = create(TA, "ticker", [N("trigger", "trigger", {"kind": "schedule", "interval_seconds": 1}), N("t", "set", {"values": {"at": "{{ trigger.scheduled_for }}"}})], [E("trigger", "t")])
    check("a schedule workflow doesn't run until it's enabled", runs_of(TA, sched) == [])
    r = TA.call("PATCH", f"/api/workflows/{sched['id']}/enabled", json={"enabled": True}).json()
    check("enabling computes next_run_at", bool(r["next_run_at"]))
    got = wait_for(lambda: len(runs_of(TA, sched)) >= 3, 10)
    scheduled = runs_of(TA, sched)
    check("a 1-second schedule produced runs on its own (trigger_type 'schedule', each carrying the time it was due)", got and all(x["trigger_type"] == "schedule" for x in scheduled) and all(x["trigger_data"]["scheduled_for"] for x in scheduled), str(len(scheduled)))
    TA.call("PATCH", f"/api/workflows/{sched['id']}/enabled", json={"enabled": False})
    time.sleep(2.5)
    after_disable = len(runs_of(TA, sched))
    time.sleep(2.2)
    check("disabling stops it: no further runs", len(runs_of(TA, sched)) == after_disable)
    cron = create(TA, "cron-demo", [N("trigger", "trigger", {"kind": "schedule", "cron": "*/5 * * * *"}), N("t", "set", {"values": {"a": 1}})], [E("trigger", "t")])
    nxt = TA.call("PATCH", f"/api/workflows/{cron['id']}/enabled", json={"enabled": True}).json()["next_run_at"]
    nxt_dt = datetime.fromisoformat(nxt)
    check("a cron schedule computes its next fire time on the 5-minute boundary, in the future", nxt_dt > datetime.utcnow() and nxt_dt.minute % 5 == 0 and nxt_dt.second == 0, nxt)
    TA.call("PATCH", f"/api/workflows/{cron['id']}/enabled", json={"enabled": False})

    # ---- two replicas, one schedule
    v.start_slate(REPLICA_PORT, name="slate-replica", env=SERVER_ENV)
    twin = create(TA, "twin-ticker", [N("trigger", "trigger", {"kind": "schedule", "interval_seconds": 2}), N("t", "set", {"values": {"at": "{{ trigger.scheduled_for }}"}})], [E("trigger", "t")])
    TA.call("PATCH", f"/api/workflows/{twin['id']}/enabled", json={"enabled": True})
    time.sleep(13)
    TA.call("PATCH", f"/api/workflows/{twin['id']}/enabled", json={"enabled": False})
    fired = [x["trigger_data"]["scheduled_for"] for x in runs_of(TA, twin)]
    check("TWO REPLICAS sharing one database (both running schedulers) fire a 2-second schedule once per tick — never twice for the same instant",
          len(fired) >= 4 and len(set(fired)) == len(fired), f"{len(fired)} runs, {len(set(fired))} distinct")

    # ---- the claim itself, under deliberate contention
    import workflow_engine as eng
    due = create(TA, "cas-due", [N("trigger", "trigger", {"kind": "schedule", "interval_seconds": 3600}), N("t", "set", {"values": {"a": 1}})], [E("trigger", "t")])
    TA.call("PATCH", f"/api/workflows/{due['id']}/enabled", json={"enabled": True})
    db_exec("update workflows set next_run_at = ? where id = ?", ((datetime.utcnow() - timedelta(seconds=2)).strftime("%Y-%m-%d %H:%M:%S.%f"), due["id"]))
    barrier = threading.Barrier(8)

    def tick():
        barrier.wait()
        eng._scheduler_tick()

    threads = [threading.Thread(target=tick) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    time.sleep(2)
    check("COMPARE-AND-SWAP under deliberate contention: 8 scheduler ticks at the same instant (plus both live replicas) claim one due schedule → exactly ONE run",
          len(runs_of(TA, due)) == 1, f"{len(runs_of(TA, due))} runs")
    check("…and its next fire time moved an hour ahead, so nothing re-fires it", datetime.fromisoformat(TA.call("GET", f"/api/workflows/{due['id']}").json()["next_run_at"]) > datetime.utcnow() + timedelta(minutes=50))

    # ---- events
    won = create(TA, "deal-won", [N("trigger", "trigger", {"kind": "event", "event_type": "deal.updated", "filter": {"stage": "Won"}}),
                                 N("s", "set", {"values": {"deal": "{{ trigger.data.name }}", "stage": "{{ trigger.data.stage }}"}})], [E("trigger", "s")])
    TA.call("PATCH", f"/api/workflows/{won['id']}/enabled", json={"enabled": True})
    deal = TA.call("POST", "/api/deals", json={"name": "Event Deal", "company": "Evt Co", "value": 10, "stage": "Lead"}).json()
    TA.call("PATCH", f"/api/deals/{deal['id']}", json={"stage": "Negotiation"})
    time.sleep(1.5)
    check("an event workflow with filter {stage: Won} is NOT triggered by a move to 'Negotiation'", runs_of(TA, won) == [])
    TA.call("PATCH", f"/api/deals/{deal['id']}", json={"stage": "Won"})
    got = wait_for(lambda: runs_of(TA, won), 8)
    check("moving the deal to 'Won' triggers exactly one run, carrying the event's data",
          len(got) == 1 and got[0]["trigger_type"] == "event" and got[0]["trigger_data"]["data"]["name"] == "Event Deal" and got[0]["trigger_data"]["data"]["stage"] == "Won", str(got))
    done = wait_for(lambda: (lambda r: r if r["status"] in ("succeeded", "failed") else None)(TA.call("GET", f"/api/workflows/runs/{got[0]['id']}").json()), 8)
    check("…and it ran to completion using that data", done["status"] == "succeeded" and done["output"]["s"] == {"deal": "Event Deal", "stage": "Won"}, str(done))

    loop = create(TB, "loop-guard", [N("trigger", "trigger", {"kind": "event", "event_type": "deal.updated"}),
                                    N("mk", "create_record", {"entity": "deal", "fields": {"name": "Created by workflow", "company": "Loop Co"}}), N("n", "notify", {"message": "made one"})],
                  [E("trigger", "mk"), E("mk", "n")])
    TB.call("PATCH", f"/api/workflows/{loop['id']}/enabled", json={"enabled": True})
    tb_deal = TB.call("POST", "/api/deals", json={"name": "B Deal", "company": "B Co", "value": 1, "stage": "Lead"}).json()
    TB.call("PATCH", f"/api/deals/{tb_deal['id']}", json={"stage": "Won"})
    wait_for(lambda: runs_of(TB, loop) and runs_of(TB, loop)[0]["status"] in ("succeeded", "failed"), 8)
    time.sleep(2)
    check("LOOP GUARD: a workflow that creates a deal and sends a notification (both would be events) fires once and does NOT retrigger itself",
          len(runs_of(TB, loop)) == 1 and runs_of(TB, loop)[0]["status"] == "succeeded" and db_one("select count(*) from crm_deals where tenant_id = ? and name = 'Created by workflow'", (TB.id,)) == 1)
    check("TENANT ISOLATION: workspace B's 'Won' event did not trigger workspace A's 'Won' workflow", len(runs_of(TA, won)) == 1)
    TA.set_flag("workflow_engine", False)
    deal2 = TA.call("POST", "/api/deals", json={"name": "Quiet Deal", "company": "Q", "value": 1, "stage": "Lead"}).json()
    TA.call("PATCH", f"/api/deals/{deal2['id']}", json={"stage": "Won"})
    time.sleep(1.5)
    TA.set_flag("workflow_engine", True)
    check("flag OFF: events stop triggering workflows", len(runs_of(TA, won)) == 1)

    disabled_run = TA.call("POST", f"/api/workflows/{sched['id']}/run", json={"input": {}, "wait": True}).json()
    check("a DISABLED workflow can still be run by hand (to test it before switching it on)", disabled_run["status"] == "succeeded" and disabled_run["trigger_type"] == "manual")


# ===========================================================================
@section("8. Action nodes: AI, create_record, send_email, notify")
def sec_actions():
    TA.call("PATCH", "/api/ai-studio/settings", json={"provider": "local"})
    wf = create(TA, "ai", [TRIGGER, N("ask", "ai_prompt", {"system": "You are terse.", "prompt": "Summarise: {{ trigger.text }}", "max_tokens": 50, "json_output": True})], [E("trigger", "ask")])
    mock_reset()
    r = run(TA, wf, {"text": "hello world"})
    req = mock_log("/v1/chat")[-1]
    check("AI node: asks the workspace's provider and returns its text (and no parsed JSON for non-JSON text)",
          r["status"] == "succeeded" and "MOCK-REPLY[mock-local]" in node(r, "ask")["output"]["text"] and node(r, "ask")["output"]["json"] is None, str(node(r, "ask")))
    check("…the model received the system message and the TEMPLATED prompt, max_tokens as set",
          [m["role"] for m in req["body"]["messages"]] == ["system", "user"] and req["body"]["messages"][1]["content"] == "Summarise: hello world" and req["body"]["max_tokens"] == 50)
    chain = [TRIGGER] + [N(f"ai{i}", "ai_prompt", {"prompt": f"q{i}"}) for i in range(1, 12)]
    r = run(TA, create(TA, "ai-chain", chain, [E("trigger", "ai1")] + [E(f"ai{i}", f"ai{i + 1}") for i in range(1, 11)]))
    check("LIMIT: a run can make at most 10 AI calls — the 11th node fails, the first ten succeeded",
          r["status"] == "failed" and node(r, "ai11")["status"] == "failed" and "at most 10 AI" in node(r, "ai11")["error"] and sum(n["status"] == "succeeded" for n in r["nodes"] if n["node_type"] == "ai_prompt") == 10)

    wf = create(TA, "make-records", [TRIGGER, N("deal", "create_record", {"entity": "deal", "fields": {"name": "Deal for {{ trigger.who }}", "company": "{{ trigger.co }}", "value": "{{ trigger.value }}", "stage": "Qualified"}}),
                                     N("ticket", "create_record", {"entity": "ticket", "fields": {"subject": "Follow up with {{ trigger.who }}", "priority": "High", "company": "{{ trigger.co }}"}})],
                [E("trigger", "deal"), E("deal", "ticket")])
    r = run(TA, wf, {"who": "Zed", "co": "Zed Corp", "value": 2500})
    deal_id = node(r, "deal")["output"]["id"]
    row = db_all("select name, value, stage, tenant_id, owner_user_id from crm_deals where id = ?", (deal_id,))[0]
    check("create_record deal: a real row in THIS workspace, with typed value and the stage given, owned by the workflow's creator",
          r["status"] == "succeeded" and row[:4] == ("Deal for Zed", 2500.0, "Qualified", TA.id) and row[4] is not None, str(row))
    check("create_record ticket: a real support ticket", db_one("select subject from support_tickets where id = ? and tenant_id = ?", (node(r, "ticket")["output"]["id"], TA.id)) == "Follow up with Zed")
    check("…both audited as workflow.record_created", sum(e["action"] == "workflow.record_created" for e in TA.call("GET", "/api/audit-log").json()) >= 2)
    r = run(TA, create(TA, "bad-record", [TRIGGER, N("d", "create_record", {"entity": "deal", "fields": {"name": "no company"}})], [E("trigger", "d")]))
    check("create_record with missing fields fails with a clear message", r["status"] == "failed" and "needs 'name' and 'company'" in r["error"])

    _, teammate = TA.add_user("standard_user")
    wf = create(TA, "mail", [TRIGGER, N("m", "send_email", {"to": "{{ trigger.to }}", "subject": "Hi", "body": "From a workflow"})], [E("trigger", "m")])
    r = run(TA, wf, {"to": "someone@evil.example"})
    check("EMAIL: sending to an address the workspace doesn't know (open-relay abuse) is refused",
          r["status"] == "failed" and "isn't a teammate or a CRM contact" in r["error"], r["error"] or "")
    r = run(TA, wf, {"to": teammate})
    check("…but sending to a teammate works", r["status"] == "succeeded" and node(r, "m")["output"] == {"sent": True, "to": teammate}, str(r["error"]))
    r = run(TB, create(TB, "mail-b", [TRIGGER, N("m", "send_email", {"to": teammate, "subject": "x", "body": "x"})], [E("trigger", "m")]))
    check("…and a teammate of workspace A is NOT reachable from workspace B", r["status"] == "failed" and "isn't a teammate" in r["error"])

    sub = redis.Redis(port=REDIS_PORT).pubsub()
    sub.subscribe(f"slate:events:tenant:{TA.id}")
    sub.get_message(timeout=1)
    run(TA, create(TA, "notify", [TRIGGER, N("n", "notify", {"message": "Hello team from {{ trigger.who }}"})], [E("trigger", "n")]), {"who": "the workflow"})
    seen = None
    for _ in range(20):
        m = sub.get_message(timeout=0.5)
        if m and m["type"] == "message" and b"workflow.notification" in m["data"]:
            seen = json.loads(m["data"])
            break
    check("notify: a real 'workflow.notification' event is published on the workspace's Redis channel (what WebSocket clients receive)",
          bool(seen) and seen["data"]["message"] == "Hello team from the workflow", str(seen))


# ===========================================================================
@section("9. Isolation, limits, recovery, and the production-mode SSRF guard")
def sec_security():
    wf = create(TA, "isolated", [TRIGGER, N("a", "set", {"values": {"x": 1}})], [E("trigger", "a")])
    r = run(TA, wf)
    check("workspace B: can't read, edit, enable, delete, or run A's workflow, nor read its run (404 each)",
          all(TB.call(m, p, **kw).status_code == 404 for m, p, kw in (
              ("GET", f"/api/workflows/{wf['id']}", {}), ("PUT", f"/api/workflows/{wf['id']}", {"json": {"name": "hijack"}}), ("PATCH", f"/api/workflows/{wf['id']}/enabled", {"json": {"enabled": True}}),
              ("DELETE", f"/api/workflows/{wf['id']}", {}), ("POST", f"/api/workflows/{wf['id']}/run", {"json": {}}), ("GET", f"/api/workflows/runs/{r['id']}", {}), ("GET", f"/api/workflows/{wf['id']}/runs", {}))))
    check("…B's list is only B's, and B can't see A's credentials", all(w["id"] != wf["id"] for w in TB.call("GET", "/api/workflows").json()) and TB.call("GET", "/api/workflow-credentials").json() == [])
    TA.call("POST", "/api/workflow-credentials", json={"name": "a-only", "kind": "bearer", "secret": "A-ONLY-SECRET", "allowed_hosts": ["127.0.0.1"]})
    r = run(TB, create(TB, "steal", [TRIGGER, N("c", "http_request", {"method": "GET", "url": f"{MOCK}/echo", "credential": "a-only"})], [E("trigger", "c")]))
    check("B naming A's credential gets \"doesn't exist\" — credentials are resolved inside the workspace", r["status"] == "failed" and "doesn't exist in this workspace" in r["error"] and "A-ONLY-SECRET" not in json.dumps(r))

    wf = create(TA, "redaction", [TRIGGER, N("a", "set", {"values": {"ok": "{{ trigger.name }}"}})], [E("trigger", "a")])
    r = run(TA, wf, {"name": "n", "password": "hunter2", "api_key": "k-123", "nested": {"Authorization": "Bearer zzz"}})
    check("sensitive-looking keys (password, api_key, Authorization…) are redacted in the stored trigger data", "hunter2" not in json.dumps(r) and "k-123" not in json.dumps(r) and "zzz" not in json.dumps(r) and r["trigger_data"]["password"] == "[redacted]")
    check("input over 100 KB → 400", TA.call("POST", f"/api/workflows/{wf['id']}/run", json={"input": {"blob": "x" * 150_000}}).status_code == 400)

    t0 = time.time()
    r = run(TA, create(TA, "too-long", [TRIGGER, N("d", "delay", {"seconds": 20})], [E("trigger", "d")]))
    check("RUN TIME LIMIT (8s here; 300s by default): a run that overstays is stopped and marked failed, its node cancelled",
          r["status"] == "failed" and "exceeded the 8s time limit" in r["error"] and node(r, "d")["status"] == "failed" and "Cancelled" in node(r, "d")["error"] and time.time() - t0 < 14, f"{time.time() - t0:.1f}s {r['error']}")

    wf_id = wf["id"]
    stale = (datetime.utcnow() - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S.%f")
    fresh = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
    for beat, label in ((stale, "stale"), (fresh, "fresh")):
        db_exec("insert into workflow_runs (tenant_id, workflow_id, workflow_version, status, trigger_type, definition, heartbeat_at, created_at, started_at) values (?, ?, 1, 'running', 'manual', '{}', ?, ?, ?)",
                (TA.id, wf_id, beat, beat, beat))
    time.sleep(1.5)
    rows = dict(db_all("select heartbeat_at < ?, status from workflow_runs where status in ('running', 'failed') and definition = '{}' order by id", ((datetime.utcnow() - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S.%f"),)))
    check("REAPER: a 'running' run whose heartbeat went stale (its worker died) is failed with an explanation; one with a live heartbeat is left alone",
          rows.get(1) == "failed" and rows.get(0) == "running", str(rows))
    check("…and the reaper's reason is stored", "worker running this workflow was lost" in (db_one("select error from workflow_runs where definition = '{}' and status = 'failed' limit 1") or ""))

    # production mode: the outbound guard applies to workflow HTTP nodes too
    for url, what in ((f"{MOCK}/echo", "a loopback service"), ("http://169.254.169.254/latest/meta-data/", "the cloud metadata endpoint")):
        pw = TP.call("POST", "/api/workflows", json={"name": f"ssrf-{secrets.token_hex(3)}", "definition": definition(
            [TRIGGER, N("c", "http_request", {"method": "GET", "url": url}, retry={"max_attempts": 3, "backoff_seconds": 0})], [E("trigger", "c")])}).json()
        r = TP.call("POST", f"/api/workflows/{pw['id']}/run", json={"input": {}, "wait": True}, timeout=60).json()
        check(f"PRODUCTION: an HTTP node aimed at {what} fails ('private or internal address'), isn't retried, and nothing connects",
              r["status"] == "failed" and "private or internal" in r["error"] and node(r, "c")["attempts"] == 1, r["error"] or "")

    gone = create(TA, "busy-delete", [TRIGGER, N("d", "delay", {"seconds": 3})], [E("trigger", "d")])
    TA.call("POST", f"/api/workflows/{gone['id']}/run", json={"input": {}})
    time.sleep(0.5)
    check("deleting a workflow while a run is in progress → 409", TA.call("DELETE", f"/api/workflows/{gone['id']}").status_code == 409)
    wait_for(lambda: all(x["status"] in ("succeeded", "failed") for x in runs_of(TA, gone)), 8)
    check("…and it can be deleted afterwards, with its runs", TA.call("DELETE", f"/api/workflows/{gone['id']}").status_code == 200 and db_one("select count(*) from workflow_runs where workflow_id = ?", (gone["id"],)) == 0)

    actions = [e["action"] for e in TA.call("GET", "/api/audit-log").json()]
    check("the audit log records workflow lifecycle and credential events — and none contains a webhook or credential secret",
          {"workflow.created", "workflow.updated", "workflow.enabled", "workflow.disabled", "workflow.run_started", "workflow.webhook_secret_rotated", "workflow_credential.created"} <= set(actions)
          and CRED_SECRET not in json.dumps(TA.call("GET", "/api/audit-log").json()))


# ===========================================================================
def setup():
    global TA, TB, TF, TP
    v.start_redis()
    v.start_mock()
    v.start_slate(env=SERVER_ENV)
    v.start_slate(PROD_PORT, name="slate-prod", db_path=PROD_DB, env={**SERVER_ENV, "ENVIRONMENT": "production", "SECRET_KEY": secrets.token_urlsafe(32), "FIELD_ENCRYPTION_KEY": Fernet.generate_key().decode()})
    TA = Tenant("Verify WF A", f"admin-a-{secrets.token_hex(3)}@example.com")
    TB = Tenant("Verify WF B", f"admin-b-{secrets.token_hex(3)}@example.com")
    TF = Tenant("Verify WF Off", f"admin-f-{secrets.token_hex(3)}@example.com")
    TP = Tenant("Verify WF Prod", f"admin-p-{secrets.token_hex(3)}@example.com", port=PROD_PORT, db_path=PROD_DB)
    for t in (TA, TB):
        t.set_flag("workflow_engine", True)
    assert TP.call("PATCH", "/api/feature-flags/workflow_engine", json={"enabled": True}).status_code == 200


if __name__ == "__main__":
    sys.exit(v.run("Workflow engine", setup, log_names=("slate", "slate-replica", "slate-prod")))
