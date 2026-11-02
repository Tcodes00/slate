"""
Workflow engine — Automation Studio's n8n/Zapier-class automation.

A workflow is a directed acyclic graph of nodes. One trigger node starts it (manual, signed webhook,
schedule, or an internal event like a deal moving to Won); action nodes do the work (HTTP request, AI
prompt, create a deal or ticket, send email, notify the team); logic nodes shape the flow (condition,
switch, merge, delay, fail). Branches that aren't taken are skipped, independent branches run in
parallel, and a node can retry with backoff, time out, and either stop the run, carry on, or route to an
"error" branch.

Design decisions that matter, in the order someone reviewing this would ask about them:

  * No code execution. Values are wired together with a tiny template language — {{ path.to.value }}
    with a handful of filters — that can only *read* from the run's data. It is parsed by hand and never
    reaches eval(), exec(), or attribute access, so `{{ __import__('os') }}` is just a path that resolves
    to nothing. (A "run my JavaScript" node is what other tools ship and is where their worst security
    reports come from; it's deliberately absent.)
  * Every outbound call goes through outbound.py (resolve, check, pin, no redirects, bounded response),
    because the URL is whatever the person building the workflow typed.
  * Secrets live in a credential vault (WorkflowCredential): encrypted at rest, bound to the hosts they
    may be sent to, injected at the last moment, never present in a definition, a run record, or an API
    response. Stored node inputs/outputs are additionally redacted by key name.
  * Runs are durable and debuggable: every run snapshots the definition it ran, and every node records its
    rendered input, output, attempts, and error. A heartbeat marks a run alive; the scheduler reaps runs
    whose worker died. (Multi-replica safe: it never fails "everything running" at startup.)
  * Schedules are claimed with a compare-and-swap on next_run_at, so N replicas fire a schedule once.
  * Hard limits everywhere: nodes per workflow, run time, concurrent runs per tenant, HTTP/AI/email calls
    per run, bytes stored per node.

Honest limits: runs execute in-process on a thread pool, not on a durable queue (Celery/Arq/SQS) — a
process restart fails its in-flight runs rather than resuming them; there's no loop/iterate node yet; and
the editor is a JSON/definition editor, not a drag-and-drop canvas.
"""

import asyncio
import base64
import json
import logging
import operator
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import urlparse

from croniter import croniter
from sqlalchemy import func

import ai_providers
import events
import feature_flags
import models
import outbound
from database import SessionLocal
from security import decrypt_field, log_audit

logger = logging.getLogger("slate.workflows")

MAX_NODES = 50
MAX_RUN_SECONDS = int(os.getenv("WORKFLOW_MAX_RUN_SECONDS", "300"))
MAX_CONCURRENT_PER_TENANT = int(os.getenv("WORKFLOW_MAX_CONCURRENT", "5"))
MIN_INTERVAL_SECONDS = int(os.getenv("WORKFLOW_MIN_INTERVAL", "60"))
WORKER_THREADS = int(os.getenv("WORKFLOW_WORKERS", "8"))
SCHEDULER_TICK_SECONDS = float(os.getenv("WORKFLOW_SCHEDULER_TICK", "1"))
HEARTBEAT_SECONDS = float(os.getenv("WORKFLOW_HEARTBEAT", "5"))
STALE_AFTER_SECONDS = int(os.getenv("WORKFLOW_STALE_AFTER", "60"))
STORE_LIMIT = 64_000
CALL_LIMITS = {"http": 25, "ai": 10, "email": 20}

_executor = ThreadPoolExecutor(max_workers=WORKER_THREADS, thread_name_prefix="workflow")


class NodeError(Exception):
    def __init__(self, message: str, retryable: bool = False, detail: Optional[dict] = None):
        super().__init__(message)
        self.retryable = retryable
        self.detail = detail or {}


class Busy(Exception):
    """The workspace already has its maximum number of runs in flight."""


# ===========================================================================
# Expressions: {{ path.to[0].value | filter }} — read-only, hand-parsed, never evaluated
# ===========================================================================

MISSING = object()
_TEMPLATE_RE = re.compile(r"\{\{(.*?)\}\}", re.S)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-]*")
_INDEX_RE = re.compile(r"\[(?:(\d{1,6})|\"([^\"]{1,60})\"|'([^']{1,60})')\]")
_MAX_RENDERED = 1_000_000


def _tokens(path: str):
    tokens, i = [], 0
    while i < len(path):
        if path[i] == ".":
            m = _IDENT_RE.match(path, i + 1)
            if not m:
                return None
            tokens.append(m.group(0))
            i = m.end()
        elif path[i] == "[":
            m = _INDEX_RE.match(path, i)
            if not m:
                return None
            tokens.append(int(m.group(1)) if m.group(1) is not None else (m.group(2) or m.group(3)))
            i = m.end()
        elif i == 0:
            m = _IDENT_RE.match(path, 0)
            if not m:
                return None
            tokens.append(m.group(0))
            i = m.end()
        else:
            return None
    return tokens or None


def resolve_path(path: str, ctx: dict):
    path = path.strip()
    if not path or len(path) > 200:
        return MISSING
    tokens = _tokens(path)
    if tokens is None:
        return MISSING
    cur: Any = ctx
    for token in tokens:
        if isinstance(token, int):
            if isinstance(cur, list) and token < len(cur):
                cur = cur[token]
            else:
                return MISSING
        elif isinstance(cur, dict) and token in cur:
            cur = cur[token]
        else:
            return MISSING
    return cur


def _split_filters(expr: str) -> list:
    parts, buf, quote = [], [], None
    for ch in expr:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch == "|":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def _to_text(value) -> str:
    if value is MISSING or value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def _num(value):
    if isinstance(value, bool) or value is MISSING or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_FILTERS = {
    "upper": lambda v: _to_text(v).upper(), "lower": lambda v: _to_text(v).lower(), "trim": lambda v: _to_text(v).strip(),
    "length": lambda v: len(v) if isinstance(v, (str, list, dict)) else 0, "json": lambda v: json.dumps(None if v is MISSING else v, default=str),
    "string": _to_text, "int": lambda v: int(_num(v)) if _num(v) is not None else None, "float": _num,
}


def _apply_filter(spec: str, value):
    m = re.match(r"^(\w+)(?:\((.*)\))?$", spec.strip(), re.S)
    if not m:
        return value
    name, arg = m.group(1), m.group(2)
    if name == "default":
        if value is MISSING or value is None or value == "":
            try:
                return json.loads(arg) if arg is not None else None
            except ValueError:
                return (arg or "").strip("\"'")
        return value
    fn = _FILTERS.get(name)
    return fn(value) if fn else value


def evaluate(expr: str, ctx: dict):
    parts = _split_filters(expr)
    value = resolve_path(parts[0], ctx)
    for spec in parts[1:]:
        value = _apply_filter(spec, value)
    return value


def render(value, ctx: dict):
    """Resolves {{ }} in strings, recursively through dicts and lists. A string that is exactly one
    expression keeps its type (a number stays a number); otherwise values are spliced in as text.
    Missing paths become null / empty — they never raise and never execute anything."""
    if isinstance(value, str):
        matches = list(_TEMPLATE_RE.finditer(value))
        if not matches:
            return value
        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            result = evaluate(matches[0].group(1), ctx)
            return None if result is MISSING else result
        out, last = [], 0
        for m in matches:
            out.append(value[last:m.start()])
            out.append(_to_text(evaluate(m.group(1), ctx)))
            last = m.end()
        out.append(value[last:])
        text = "".join(out)
        return text if len(text) <= _MAX_RENDERED else text[:_MAX_RENDERED]
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value


_SENSITIVE_KEY = re.compile(r"(authorization|api[-_]?key|secret|token|password|passwd|cookie|signature)", re.I)


def redact(obj, depth: int = 0):
    if depth > 8:
        return "[truncated]"
    if isinstance(obj, dict):
        return {k: ("[redacted]" if isinstance(k, str) and _SENSITIVE_KEY.search(k) else redact(v, depth + 1)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, depth + 1) for v in obj[:200]]
    if isinstance(obj, str) and len(obj) > 20_000:
        return obj[:20_000] + "…[truncated]"
    return obj


def store(obj) -> str:
    """What gets persisted for display: redacted by key name, and bounded."""
    text = json.dumps(redact(obj), default=str)
    return text if len(text) <= STORE_LIMIT else json.dumps({"_truncated": True, "preview": text[:STORE_LIMIT - 200]})


_COMPARATORS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}
OPS = ("==", "!=", ">", ">=", "<", "<=", "contains", "not_contains", "starts_with", "ends_with", "exists", "not_exists", "in")


def _loose_eq(a, b) -> bool:
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return na == nb
    return a == b if (a is None or b is None) else _to_text(a) == _to_text(b)


def eval_condition(left, op: str, right) -> bool:
    if op == "exists":
        return left is not None and left != ""
    if op == "not_exists":
        return left is None or left == ""
    if op == "==":
        return _loose_eq(left, right)
    if op == "!=":
        return not _loose_eq(left, right)
    if op in _COMPARATORS:
        a, b = _num(left), _num(right)
        return a is not None and b is not None and _COMPARATORS[op](a, b)
    if op in ("contains", "not_contains"):
        if isinstance(left, (list, tuple)):
            found = any(_loose_eq(item, right) for item in left)
        elif isinstance(left, dict):
            found = _to_text(right) in left
        else:
            found = _to_text(right) in _to_text(left)
        return found if op == "contains" else not found
    if op == "starts_with":
        return _to_text(left).startswith(_to_text(right))
    if op == "ends_with":
        return _to_text(left).endswith(_to_text(right))
    if op == "in":
        if isinstance(right, (list, tuple)):
            return any(_loose_eq(left, item) for item in right)
        return _to_text(left) in _to_text(right)
    return False


# ===========================================================================
# Definition validation
# ===========================================================================

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
TRIGGER_KINDS = ("manual", "webhook", "schedule", "event")
ON_ERROR = ("stop", "continue", "branch")

NODE_CATALOG = {
    "trigger": {"label": "Trigger", "description": "Starts the workflow: manual, webhook, schedule, or an internal event.",
                "params": ["kind", "interval_seconds", "cron", "event_type", "filter", "require_signature"]},
    "set": {"label": "Set values", "description": "Builds an object from templates — rename, combine, or reshape data.", "params": ["values"]},
    "http_request": {"label": "HTTP request", "description": "Calls an external API, optionally with a stored credential.",
                     "params": ["method", "url", "headers", "query", "body", "credential", "timeout_seconds", "expect_status"]},
    "condition": {"label": "Condition", "description": "Branches 'true' / 'false'.", "params": ["left", "op", "right", "conditions", "mode"], "branches": ["true", "false"]},
    "switch": {"label": "Switch", "description": "Branches by matching a value against cases, else 'default'.", "params": ["value", "cases"], "branches": ["<case label>", "default"]},
    "ai_prompt": {"label": "AI prompt", "description": "Asks the workspace's AI provider; returns its text.", "params": ["prompt", "system", "max_tokens", "json_output"]},
    "rag_search": {"label": "Search knowledge", "description": "Searches the workspace's indexed documents; returns the best passages (no AI call).", "params": ["query", "top_k"]},
    "rag_query": {"label": "Ask knowledge", "description": "Answers a question from the workspace's indexed documents, with citations.", "params": ["question", "top_k"]},
    "create_record": {"label": "Create record", "description": "Creates a deal or a support ticket.", "params": ["entity", "fields"]},
    "send_email": {"label": "Send email", "description": "Emails a teammate or a CRM contact (never arbitrary addresses).", "params": ["to", "subject", "body"]},
    "notify": {"label": "Notify team", "description": "Pushes a live notification to everyone online in the workspace.", "params": ["message"]},
    "delay": {"label": "Delay", "description": "Waits up to 30 seconds.", "params": ["seconds"]},
    "merge": {"label": "Merge", "description": "Waits for all branches that ran, then continues with their combined output.", "params": []},
    "fail": {"label": "Fail", "description": "Stops the run with an error message.", "params": ["message"]},
}


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_node_params(node: dict) -> list:
    errors, nid, t = [], node["id"], node["type"]
    p = node.get("params")
    p = p if isinstance(p, dict) else {}

    def need(name, kind=None):
        if name not in p or p[name] in (None, ""):
            errors.append(f"Node '{nid}' ({t}) needs '{name}'")
            return False
        if kind and not isinstance(p[name], kind):
            errors.append(f"Node '{nid}': '{name}' must be {kind.__name__ if isinstance(kind, type) else 'the right type'}")
            return False
        return True

    if t == "trigger":
        kind = p.get("kind", "manual")
        if kind not in TRIGGER_KINDS:
            errors.append(f"Trigger kind must be one of: {', '.join(TRIGGER_KINDS)}")
        elif kind == "schedule":
            interval, cron = p.get("interval_seconds"), p.get("cron")
            if (interval is None) == (cron is None):
                errors.append("A schedule trigger needs exactly one of 'interval_seconds' or 'cron'")
            elif interval is not None and (not _is_number(interval) or interval < MIN_INTERVAL_SECONDS):
                errors.append(f"'interval_seconds' must be a number of at least {MIN_INTERVAL_SECONDS}")
            elif cron is not None and (not isinstance(cron, str) or not croniter.is_valid(cron)):
                errors.append("'cron' isn't a valid cron expression")
        elif kind == "event":
            if need("event_type", str) and "filter" in p and not isinstance(p["filter"], dict):
                errors.append("'filter' must be an object of field → value")
    elif t == "set":
        need("values", dict)
    elif t == "http_request":
        if need("url", str):
            pass
        if p.get("method", "GET") not in HTTP_METHODS:
            errors.append(f"Node '{nid}': method must be one of {', '.join(HTTP_METHODS)}")
        for field in ("headers", "query"):
            if field in p and not isinstance(p[field], dict):
                errors.append(f"Node '{nid}': '{field}' must be an object")
        if "timeout_seconds" in p and (not _is_number(p["timeout_seconds"]) or not 1 <= p["timeout_seconds"] <= 30):
            errors.append(f"Node '{nid}': timeout_seconds must be between 1 and 30")
        if "credential" in p and not isinstance(p["credential"], str):
            errors.append(f"Node '{nid}': credential must be a credential name")
    elif t == "condition":
        if "conditions" in p:
            conds = p["conditions"]
            if not isinstance(conds, list) or not conds or any(not isinstance(c, dict) or c.get("op") not in OPS for c in conds):
                errors.append(f"Node '{nid}': 'conditions' must be a list of {{left, op, right}} with op in {', '.join(OPS)}")
            if p.get("mode", "and") not in ("and", "or"):
                errors.append(f"Node '{nid}': mode must be 'and' or 'or'")
        elif p.get("op") not in OPS or "left" not in p:
            errors.append(f"Node '{nid}': a condition needs 'left' and an 'op' from: {', '.join(OPS)}")
    elif t == "switch":
        cases = p.get("cases")
        if "value" not in p:
            errors.append(f"Node '{nid}' (switch) needs 'value'")
        if not isinstance(cases, list) or not cases or any(not isinstance(c, dict) or not isinstance(c.get("label"), str) or not c["label"] or c["label"] == "default" or "equals" not in c for c in cases):
            errors.append(f"Node '{nid}': 'cases' must be a list of {{label, equals}} (labels non-empty and not 'default')")
        elif len({c["label"] for c in cases}) != len(cases):
            errors.append(f"Node '{nid}': case labels must be unique")
    elif t == "ai_prompt":
        need("prompt", str)
        if "max_tokens" in p and (not _is_number(p["max_tokens"]) or not 1 <= p["max_tokens"] <= 2000):
            errors.append(f"Node '{nid}': max_tokens must be between 1 and 2000")
    elif t in ("rag_search", "rag_query"):
        need("query" if t == "rag_search" else "question")
        if "top_k" in p and (not _is_number(p["top_k"]) or not 1 <= p["top_k"] <= 20):
            errors.append(f"Node '{nid}': top_k must be between 1 and 20")
    elif t == "create_record":
        if p.get("entity") not in ("deal", "ticket"):
            errors.append(f"Node '{nid}': entity must be 'deal' or 'ticket'")
        need("fields", dict)
    elif t == "send_email":
        for name in ("to", "subject", "body"):
            need(name)
    elif t == "notify":
        need("message")
    elif t == "delay":
        if "seconds" not in p or not _is_number(p["seconds"]) or not 0 <= p["seconds"] <= 30:
            errors.append(f"Node '{nid}': delay seconds must be between 0 and 30")
    elif t == "fail":
        need("message")

    retry = node.get("retry")
    if retry is not None:
        if not isinstance(retry, dict) or not _is_number(retry.get("max_attempts", 1)) or not 1 <= retry.get("max_attempts", 1) <= 5 \
                or not _is_number(retry.get("backoff_seconds", 1)) or not 0 <= retry.get("backoff_seconds", 1) <= 30:
            errors.append(f"Node '{nid}': retry needs max_attempts 1-5 and backoff_seconds 0-30")
    if "timeout_seconds" in node and (not _is_number(node["timeout_seconds"]) or not 1 <= node["timeout_seconds"] <= 120):
        errors.append(f"Node '{nid}': timeout_seconds must be between 1 and 120")
    if node.get("on_error", "stop") not in ON_ERROR:
        errors.append(f"Node '{nid}': on_error must be one of {', '.join(ON_ERROR)}")
    return errors


def validate_definition(defn) -> list:
    """Every reason a definition can't be saved, all at once — an editor wants the whole list."""
    if not isinstance(defn, dict):
        return ["The definition must be an object with 'nodes' and 'edges'"]
    nodes, edges = defn.get("nodes"), defn.get("edges", [])
    if not isinstance(nodes, list) or not nodes:
        return ["The definition needs a non-empty 'nodes' list"]
    if not isinstance(edges, list):
        return ["'edges' must be a list"]
    if len(nodes) > MAX_NODES:
        return [f"A workflow can have at most {MAX_NODES} nodes"]

    errors, by_id = [], {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not _ID_RE.match(node["id"]):
            errors.append("Every node needs an 'id' of 1-40 letters, digits, '-' or '_'")
            continue
        if node["id"] in by_id:
            errors.append(f"Duplicate node id '{node['id']}'")
            continue
        if node.get("type") not in NODE_CATALOG:
            errors.append(f"Node '{node['id']}' has unknown type '{node.get('type')}' (known: {', '.join(NODE_CATALOG)})")
            continue
        by_id[node["id"]] = node
        errors.extend(_validate_node_params(node))
    if errors:
        return errors

    triggers = [n for n in by_id.values() if n["type"] == "trigger"]
    if len(triggers) != 1:
        errors.append(f"A workflow needs exactly one trigger node (found {len(triggers)})")
    for n in by_id.values():
        if n["id"] in ("trigger", "run") and n["type"] != "trigger" or n["id"] == "run":
            errors.append(f"Node id '{n['id']}' is reserved")

    incoming = {nid: 0 for nid in by_id}
    adjacency = {nid: [] for nid in by_id}
    for e in edges:
        if not isinstance(e, dict) or e.get("from") not in by_id or e.get("to") not in by_id:
            errors.append(f"Edge {e!r} must connect two existing nodes ('from' and 'to')")
            continue
        src, dst, branch = by_id[e["from"]], by_id[e["to"]], e.get("branch")
        if e["from"] == e["to"]:
            errors.append(f"Node '{e['from']}' can't connect to itself")
            continue
        if dst["type"] == "trigger":
            errors.append("The trigger can't have incoming edges")
        valid = {None}
        if src["type"] == "condition":
            valid = {"true", "false"}
        elif src["type"] == "switch":
            valid = {c["label"] for c in src["params"]["cases"]} | {"default"}
        if src.get("on_error") == "branch":
            valid = valid | {"error"}
        if branch not in valid:
            errors.append(f"Edge {e['from']} → {e['to']}: branch {branch!r} isn't valid for a '{src['type']}' node"
                          + (f" (use: {', '.join(sorted(b for b in valid if b))})" if valid != {None} else " (it has no branches)"))
        incoming[e["to"]] += 1
        adjacency[e["from"]].append(e["to"])
    if errors:
        return errors

    # acyclic + everything reachable from the trigger
    remaining = dict(incoming)
    queue = [nid for nid, n in remaining.items() if n == 0]
    visited = 0
    while queue:
        nid = queue.pop()
        visited += 1
        for nxt in adjacency[nid]:
            remaining[nxt] -= 1
            if remaining[nxt] == 0:
                queue.append(nxt)
    if visited != len(by_id):
        errors.append("The graph contains a cycle — workflows must be acyclic")
        return errors
    if triggers:
        seen, stack = set(), [triggers[0]["id"]]
        while stack:
            nid = stack.pop()
            if nid in seen:
                continue
            seen.add(nid)
            stack.extend(adjacency[nid])
        for nid in by_id:
            if nid not in seen:
                errors.append(f"Node '{nid}' isn't connected to the trigger")
    return errors


def trigger_from_definition(defn: dict):
    node = next(n for n in defn["nodes"] if n["type"] == "trigger")
    p = node.get("params") or {}
    kind = p.get("kind", "manual")
    cfg: dict = {}
    if kind == "schedule":
        cfg = {k: p[k] for k in ("interval_seconds", "cron") if p.get(k) is not None}
    elif kind == "event":
        cfg = {"event_type": p["event_type"], "filter": p.get("filter") or {}}
    elif kind == "webhook":
        cfg = {"require_signature": p.get("require_signature", True) is not False}
    return kind, cfg


def next_fire(cfg: dict, base: datetime) -> datetime:
    if cfg.get("cron"):
        return croniter(cfg["cron"], base).get_next(datetime)
    return base + timedelta(seconds=int(cfg["interval_seconds"]))


def referenced_credentials(defn: dict) -> set:
    return {n["params"]["credential"] for n in defn["nodes"] if n["type"] == "http_request" and isinstance((n.get("params") or {}).get("credential"), str)}


# ===========================================================================
# Node handlers: async (output, branch)
# ===========================================================================

class RunState:
    def __init__(self, tenant_id: int, run_id: int, workflow_id: int, created_by: Optional[int]):
        self.tenant_id, self.run_id, self.workflow_id, self.created_by = tenant_id, run_id, workflow_id, created_by
        self.calls = {"http": 0, "ai": 0, "email": 0}

    def count(self, kind: str, label: str) -> None:
        self.calls[kind] += 1
        if self.calls[kind] > CALL_LIMITS[kind]:
            raise NodeError(f"A single run can make at most {CALL_LIMITS[kind]} {label} calls")


def _host_allowed(host: str, allowed: list) -> bool:
    host = host.lower()
    for pattern in allowed:
        pattern = str(pattern).lower().strip()
        if pattern.startswith("*.") and host.endswith(pattern[1:]) and host != pattern[2:]:
            return True
        if pattern == host:
            return True
    return False


def _apply_credential(tenant_id: int, name: str, url: str, headers: dict) -> None:
    db = SessionLocal()
    try:
        cred = db.query(models.WorkflowCredential).filter(models.WorkflowCredential.tenant_id == tenant_id, models.WorkflowCredential.name == name).first()
        if cred is None:
            raise NodeError(f"Credential '{name}' doesn't exist in this workspace")
        host = urlparse(url).hostname or ""
        if not _host_allowed(host, json.loads(cred.allowed_hosts or "[]")):
            raise NodeError(f"Credential '{name}' isn't allowed to be sent to {host or 'that host'} — add it to the credential's allowed hosts")
        secret = decrypt_field(cred.secret_encrypted)
        if cred.kind == "bearer":
            headers["Authorization"] = f"Bearer {secret}"
        elif cred.kind == "header":
            headers[cred.header_name or "X-API-Key"] = secret
        elif cred.kind == "basic":
            headers["Authorization"] = "Basic " + base64.b64encode(f"{cred.username or ''}:{secret}".encode()).decode()
    finally:
        db.close()


_RESERVED_HEADERS = {"host", "content-length", "connection", "transfer-encoding"}


async def _h_trigger(p, ctx, st, **kw):
    return ctx["trigger"], None


async def _h_set(p, ctx, st, **kw):
    return (p.get("values") if isinstance(p.get("values"), dict) else {}), None


async def _h_http(p, ctx, st, **kw):
    st.count("http", "HTTP")
    method = str(p.get("method") or "GET").upper()
    url = p.get("url")
    if method not in HTTP_METHODS or not isinstance(url, str) or not url:
        raise NodeError("An HTTP node needs a method and a url")
    headers = {str(k): _to_text(v) for k, v in (p.get("headers") or {}).items() if str(k).lower() not in _RESERVED_HEADERS}
    query = {str(k): _to_text(v) for k, v in (p.get("query") or {}).items()}
    if p.get("credential"):
        _apply_credential(st.tenant_id, str(p["credential"]), url, headers)
    body = p.get("body")
    kwargs = {}
    if isinstance(body, (dict, list)):
        kwargs["json"] = body
    elif body is not None:
        kwargs["content"] = _to_text(body).encode()
    try:
        resp = await outbound.safe_request(method, url, headers=headers, params=query or None, timeout=min(max(float(p.get("timeout_seconds") or 15), 1), 30),
                                           max_bytes=1_000_000, label="HTTP request", **kwargs)
    except outbound.OutboundError as e:
        raise NodeError(str(e), retryable=e.retryable)
    ctype = resp.headers.get("content-type", "")
    parsed: Any = resp.text()[:20_000]
    if "json" in ctype:
        try:
            parsed = resp.json()
        except ValueError:
            pass
    out = {"status": resp.status_code, "ok": resp.status_code < 400, "body": parsed,
           "headers": {k: v for k, v in resp.headers.items() if k in ("content-type", "location", "retry-after", "x-request-id")}}
    expect = p.get("expect_status")
    good = resp.status_code in expect if isinstance(expect, list) and expect else resp.status_code < 400
    if not good:
        raise NodeError(f"HTTP {resp.status_code} from {urlparse(url).hostname}", retryable=resp.status_code == 429 or resp.status_code >= 500, detail=out)
    return out, None


async def _h_condition(p, ctx, st, **kw):
    if isinstance(p.get("conditions"), list):
        results = [eval_condition(c.get("left"), c.get("op"), c.get("right")) for c in p["conditions"]]
        result = all(results) if p.get("mode", "and") == "and" else any(results)
    else:
        result = eval_condition(p.get("left"), p.get("op", "=="), p.get("right"))
    return {"result": result}, ("true" if result else "false")


async def _h_switch(p, ctx, st, **kw):
    for case in p.get("cases") or []:
        if _loose_eq(p.get("value"), case.get("equals")):
            return {"matched": case["label"]}, case["label"]
    return {"matched": "default"}, "default"


def _parse_json_text(text: str):
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(cleaned)
    except ValueError:
        return None


async def _h_ai(p, ctx, st, **kw):
    from routers.ai_studio import get_effective_provider_model  # late import: routers import this package's models
    st.count("ai", "AI")
    db = SessionLocal()
    try:
        tenant = db.query(models.Tenant).filter(models.Tenant.id == st.tenant_id).first()
        provider, model = get_effective_provider_model(db, tenant)
    finally:
        db.close()
    if not ai_providers.is_configured(provider):
        raise NodeError(f"The workspace's AI provider ({provider}) isn't configured on the server")
    messages = ([{"role": "system", "content": _to_text(p["system"])}] if p.get("system") else []) + [{"role": "user", "content": _to_text(p.get("prompt"))}]
    try:
        text = await ai_providers.call_llm(provider, model, messages, int(min(max(float(p.get("max_tokens") or 500), 1), 2000)))
    except (RuntimeError, ValueError) as e:
        raise NodeError(str(e), retryable=True)
    out = {"text": text, "provider": provider.split(":")[0], "model": model}
    if p.get("json_output"):
        out["json"] = _parse_json_text(text)
    return out, None


async def _rag_node(p, st, answer: bool):
    import rag
    db = SessionLocal()
    try:
        if not feature_flags.is_enabled(db, st.tenant_id, "knowledge_rag"):
            raise NodeError("Knowledge search & answers is turned off for this workspace")
        top_k = int(_clamp(p.get("top_k"), 1, 20, 5))
        # Workflows retrieve as an ordinary workspace member: documents restricted to managers or admins stay out of reach.
        if not answer:
            found = await rag.search(db, st.tenant_id, "standard_user", _to_text(p.get("query")), top_k)
            return {"results": [{k: r[k] for k in ("title", "page", "text", "score", "document_id")} for r in found["results"]]}, None
        st.count("ai", "AI")
        tenant = db.query(models.Tenant).filter(models.Tenant.id == st.tenant_id).first()
        res = await rag.ask(db, tenant, "standard_user", _to_text(p.get("question")), top_k)
        return {"answer": res["answer"], "model_called": res["model_called"], "reason": res.get("reason"),
                "citations": [{"n": c["n"], "title": c["title"], "page": c["page"], "cited": c["cited"]} for c in res["citations"]]}, None
    except rag.RagError as e:
        raise NodeError(str(e), retryable=e.status >= 502)
    finally:
        db.close()


async def _h_rag_search(p, ctx, st, **kw):
    return await _rag_node(p, st, answer=False)


async def _h_rag_query(p, ctx, st, **kw):
    return await _rag_node(p, st, answer=True)


async def _h_create_record(p, ctx, st, **kw):
    fields = p.get("fields") or {}
    db = SessionLocal()
    try:
        if p["entity"] == "deal":
            if not fields.get("name") or not fields.get("company"):
                raise NodeError("A deal needs 'name' and 'company'")
            party = models.Party(tenant_id=st.tenant_id, party_type="organization", display_name=_to_text(fields["company"])[:200], owner_user_id=st.created_by)
            db.add(party)
            db.flush()
            value = _num(fields.get("value")) or 0.0
            deal = models.Deal(tenant_id=st.tenant_id, party_id=party.id, name=_to_text(fields["name"])[:200], value=value,
                               stage=_to_text(fields.get("stage") or "Lead")[:40], owner_user_id=st.created_by)
            db.add(deal)
            db.flush()
            db.add(models.DealStageHistory(tenant_id=st.tenant_id, deal_id=deal.id, from_stage=None, to_stage=deal.stage))
            record_id = deal.id
        else:
            if not fields.get("subject"):
                raise NodeError("A ticket needs a 'subject'")
            party_id = None
            if fields.get("company"):
                party = models.Party(tenant_id=st.tenant_id, party_type="organization", display_name=_to_text(fields["company"])[:200], owner_user_id=st.created_by)
                db.add(party)
                db.flush()
                party_id = party.id
            ticket = models.Ticket(tenant_id=st.tenant_id, party_id=party_id, subject=_to_text(fields["subject"])[:200], priority=_to_text(fields.get("priority") or "Normal")[:20])
            db.add(ticket)
            db.flush()
            record_id = ticket.id
        log_audit(db, st.tenant_id, None, "workflow.record_created", p["entity"], record_id, after={"workflow_run_id": st.run_id, "workflow_id": st.workflow_id})
        db.commit()
        # Deliberately NOT publishing deal.created/ticket.created from here: a workflow that creates
        # records must never be able to trigger itself (or a ring of workflows) through the event bus.
        return {"entity": p["entity"], "id": record_id}, None
    finally:
        db.close()


async def _h_send_email(p, ctx, st, **kw):
    import mailer
    st.count("email", "email")
    to = _to_text(p.get("to")).strip()
    db = SessionLocal()
    try:
        known = db.query(models.User.id).filter(models.User.tenant_id == st.tenant_id, func.lower(models.User.email) == to.lower()).first() \
            or db.query(models.Party.id).filter(models.Party.tenant_id == st.tenant_id, func.lower(models.Party.primary_email) == to.lower()).first()
    finally:
        db.close()
    if not known:
        # An unrestricted 'send email' node is an open mail relay on the platform's SMTP account.
        raise NodeError(f"'{to or '(empty)'}' isn't a teammate or a CRM contact in this workspace — workflows can only email people the workspace already knows")
    await asyncio.to_thread(mailer.send_email, to, _to_text(p.get("subject"))[:200], _to_text(p.get("body"))[:20_000])
    return {"sent": True, "to": to}, None


async def _h_notify(p, ctx, st, **kw):
    events.publish_event(st.tenant_id, "workflow.notification", {"message": _to_text(p.get("message"))[:500], "run_id": st.run_id, "workflow_id": st.workflow_id})
    return {"notified": True}, None


async def _h_delay(p, ctx, st, **kw):
    seconds = min(max(float(p.get("seconds") or 0), 0), 30)
    await asyncio.sleep(seconds)
    return {"slept": seconds}, None


async def _h_merge(p, ctx, st, outputs=None, active_sources=None, **kw):
    return {"inputs": {src: (outputs or {}).get(src) for src in (active_sources or [])}}, None


async def _h_fail(p, ctx, st, **kw):
    raise NodeError(_to_text(p.get("message")) or "The workflow stopped itself")


HANDLERS = {
    "trigger": _h_trigger, "set": _h_set, "http_request": _h_http, "condition": _h_condition, "switch": _h_switch, "ai_prompt": _h_ai,
    "rag_search": _h_rag_search, "rag_query": _h_rag_query, "create_record": _h_create_record, "send_email": _h_send_email, "notify": _h_notify, "delay": _h_delay, "merge": _h_merge, "fail": _h_fail,
}


# ===========================================================================
# Execution
# ===========================================================================

class NodeResult:
    def __init__(self, ok: bool, output, branch: Optional[str], error: Optional[str], stop_run: bool):
        self.ok, self.output, self.branch, self.error, self.stop_run = ok, output, branch, error, stop_run


def _clamp(value, low, high, default):
    try:
        return min(max(float(value), low), high)
    except (TypeError, ValueError):
        return default


async def _run_node(db, run, node: dict, ctx: dict, st: RunState, outputs: dict, active_sources: list) -> NodeResult:
    """Never raises (except cancellation): every outcome is a NodeResult plus a persisted record."""
    started = time.monotonic()
    rec = models.WorkflowNodeRun(run_id=run.id, tenant_id=run.tenant_id, node_id=node["id"], node_type=node["type"], status="running", started_at=datetime.utcnow(), attempts=0)
    db.add(rec)
    db.commit()

    retry = node.get("retry") or {}
    max_attempts = int(_clamp(retry.get("max_attempts", 1), 1, 5, 1))
    backoff = _clamp(retry.get("backoff_seconds", 1), 0, 30, 1)
    timeout = _clamp(node.get("timeout_seconds", 30), 1, 120, 30)
    on_error = node.get("on_error", "stop")
    params = render(node.get("params") or {}, ctx)
    if node["type"] == "delay":
        timeout = max(timeout, _clamp(params.get("seconds"), 0, 30, 0) + 5)
    rec.input = store(params)
    db.commit()

    output, branch, error, attempt = None, None, None, 0
    try:
        while attempt < max_attempts:
            attempt += 1
            error = None
            try:
                output, branch = await asyncio.wait_for(
                    HANDLERS[node["type"]](params, ctx, st, outputs=outputs, active_sources=active_sources), timeout=timeout)
                break
            except asyncio.TimeoutError:
                error = NodeError(f"Timed out after {timeout:g}s", retryable=True)
            except NodeError as e:
                error = e
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a node bug must fail the node, not the engine
                logger.exception("unexpected error in workflow node %s (%s)", node["id"], node["type"])
                error = NodeError("Unexpected error while running this node")
            if error.retryable and attempt < max_attempts:
                await asyncio.sleep(min(backoff * (2 ** (attempt - 1)), 10))
                continue
            break
    except asyncio.CancelledError:
        rec.status, rec.error, rec.attempts = "failed", "Cancelled: the run exceeded its time limit", attempt
        rec.finished_at, rec.duration_ms = datetime.utcnow(), int((time.monotonic() - started) * 1000)
        db.commit()
        raise

    rec.attempts, rec.finished_at, rec.duration_ms = attempt, datetime.utcnow(), int((time.monotonic() - started) * 1000)
    if error is None:
        rec.status, rec.output = "succeeded", store(output)
        db.commit()
        return NodeResult(True, output, branch, None, False)

    message = str(error)
    rec.status, rec.error = "failed", message[:2000]
    err_output = {"error": message, **error.detail}
    rec.output = store(err_output)
    db.commit()
    if on_error == "continue":
        return NodeResult(True, err_output, None, message, False)
    if on_error == "branch":
        return NodeResult(True, err_output, "error", message, False)
    return NodeResult(False, err_output, None, message, True)


def _record_skipped(db, run, node: dict, reason: str) -> None:
    db.add(models.WorkflowNodeRun(run_id=run.id, tenant_id=run.tenant_id, node_id=node["id"], node_type=node["type"], status="skipped",
                                  error=reason, attempts=0, started_at=datetime.utcnow(), finished_at=datetime.utcnow(), duration_ms=0))
    db.commit()


async def _heartbeat(run_id: int) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        db = SessionLocal()
        try:
            db.query(models.WorkflowRun).filter(models.WorkflowRun.id == run_id, models.WorkflowRun.status == "running").update({"heartbeat_at": datetime.utcnow()})
            db.commit()
        except Exception:  # noqa: BLE001
            pass
        finally:
            db.close()


async def _execute(run_id: int, trigger_data: dict) -> None:
    db = SessionLocal()
    beat = None
    try:
        run = db.get(models.WorkflowRun, run_id)
        if run is None:
            return
        wf = db.get(models.Workflow, run.workflow_id)
        run.status, run.started_at = "running", datetime.utcnow()
        run.heartbeat_at = run.started_at
        db.commit()
        beat = asyncio.create_task(_heartbeat(run_id))

        defn = json.loads(run.definition)
        nodes = {n["id"]: n for n in defn["nodes"]}
        incoming = {nid: [] for nid in nodes}
        outgoing = {nid: [] for nid in nodes}
        for e in defn["edges"]:
            incoming[e["to"]].append(e)
            outgoing[e["from"]].append(e)
        trigger_id = next(nid for nid, n in nodes.items() if n["type"] == "trigger")

        ctx: dict = {"trigger": trigger_data, "run": {"id": run.id, "workflow_id": run.workflow_id}}
        st = RunState(run.tenant_id, run.id, run.workflow_id, wf.created_by if wf else None)
        status = {nid: "pending" for nid in nodes}
        outputs: dict = {}
        resolved = {nid: 0 for nid in nodes}
        active_sources = {nid: [] for nid in nodes}
        in_flight: dict = {}
        failure = {"message": None}

        def finish(nid: str, ok: bool, branch: Optional[str]) -> None:
            for e in outgoing[nid]:
                resolved[e["to"]] += 1
                if ok and e.get("branch") == branch:
                    active_sources[e["to"]].append(nid)
            for e in outgoing[nid]:
                consider(e["to"])

        def consider(nid: str) -> None:
            if status[nid] != "pending" or resolved[nid] < len(incoming[nid]):
                return
            if failure["message"] is not None:
                skip(nid, "Not run: an earlier step failed")
            elif active_sources[nid]:
                begin(nid)
            else:
                skip(nid, "Not run: no branch leading here was taken")

        def skip(nid: str, reason: str) -> None:
            status[nid] = "skipped"
            _record_skipped(db, run, nodes[nid], reason)
            finish(nid, False, None)

        def begin(nid: str) -> None:
            status[nid] = "running"
            in_flight[asyncio.create_task(_run_node(db, run, nodes[nid], ctx, st, outputs, list(active_sources[nid])))] = nid

        async def drive() -> None:
            begin(trigger_id)
            while in_flight:
                done, _ = await asyncio.wait(list(in_flight), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    nid = in_flight.pop(task)
                    res: NodeResult = task.result()
                    status[nid] = "done"
                    if res.output is not None:
                        outputs[nid] = res.output
                        ctx[nid] = res.output
                    if res.stop_run and failure["message"] is None:
                        failure["message"] = f"Node '{nid}' failed: {res.error}"
                    finish(nid, res.ok, res.branch)

        try:
            await asyncio.wait_for(drive(), timeout=MAX_RUN_SECONDS)
        except asyncio.TimeoutError:
            for task in list(in_flight):
                task.cancel()
            await asyncio.gather(*in_flight, return_exceptions=True)
            failure["message"] = failure["message"] or f"The run exceeded the {MAX_RUN_SECONDS}s time limit"
        for nid, state in status.items():
            if state in ("pending", "running"):
                status[nid] = "skipped"
                _record_skipped(db, run, nodes[nid], "Not run: the run ended first")

        leaves = {nid: outputs[nid] for nid in nodes if not outgoing[nid] and nid in outputs}
        run.status = "failed" if failure["message"] else "succeeded"
        run.error = failure["message"]
        run.output = store(leaves)
        run.finished_at = datetime.utcnow()
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("workflow run %s crashed", run_id)
        try:
            db.rollback()
            run = db.get(models.WorkflowRun, run_id)
            if run is not None and run.status in ("queued", "running"):
                run.status, run.error, run.finished_at = "failed", "The engine hit an unexpected error running this workflow", datetime.utcnow()
                db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        if beat is not None:
            beat.cancel()
        db.close()


def _thread_main(run_id: int, trigger_data: dict) -> None:
    asyncio.run(_execute(run_id, trigger_data))


def submit(run_id: int, trigger_data: dict) -> None:
    _executor.submit(_thread_main, run_id, trigger_data)


def create_run(db, workflow: models.Workflow, trigger_type: str, trigger_data: dict, enforce_limit: bool = True) -> models.WorkflowRun:
    if enforce_limit:
        active = db.query(func.count(models.WorkflowRun.id)).filter(
            models.WorkflowRun.tenant_id == workflow.tenant_id, models.WorkflowRun.status.in_(("queued", "running"))).scalar()
        if active >= MAX_CONCURRENT_PER_TENANT:
            raise Busy(f"This workspace already has {active} workflow runs in progress (the limit is {MAX_CONCURRENT_PER_TENANT}) — try again shortly")
    now = datetime.utcnow()
    run = models.WorkflowRun(tenant_id=workflow.tenant_id, workflow_id=workflow.id, workflow_version=workflow.version, status="queued",
                             trigger_type=trigger_type, trigger_data=store(trigger_data), definition=workflow.definition, heartbeat_at=now)
    db.add(run)
    workflow.last_run_at = now
    db.commit()
    db.refresh(run)
    return run


def record_rejected(db, workflow: models.Workflow, trigger_type: str, reason: str) -> None:
    now = datetime.utcnow()
    db.add(models.WorkflowRun(tenant_id=workflow.tenant_id, workflow_id=workflow.id, workflow_version=workflow.version, status="failed", trigger_type=trigger_type,
                              trigger_data="{}", definition=workflow.definition, error=reason, created_at=now, started_at=now, finished_at=now))
    db.commit()


# ===========================================================================
# Triggers: events, schedules, and the reaper
# ===========================================================================

def _filter_matches(flt: dict, data: dict) -> bool:
    for key, expected in (flt or {}).items():
        actual = resolve_path(str(key), data if isinstance(data, dict) else {})
        if actual is MISSING or not _loose_eq(actual, expected):
            return False
    return True


def dispatch_event(tenant_id: int, event_type: str, data: dict) -> None:
    """Registered with events.add_listener, and called from inside publish_event() — which several routers
    call *before* their own commit. Doing the work right here would make this thread write a run row while
    the request's transaction still holds the database's write lock (a deadlock on SQLite, a needless
    stall elsewhere), so it only hands the event to the worker pool and returns."""
    if event_type.startswith("workflow."):
        return  # workflows' own notifications never trigger workflows
    _executor.submit(_dispatch_blocking, tenant_id, event_type, data)


def _dispatch_blocking(tenant_id: int, event_type: str, data: dict) -> None:
    try:
        _dispatch_event(tenant_id, event_type, data)
    except Exception:  # noqa: BLE001
        logger.exception("workflow event dispatch failed for %s", event_type)


def _dispatch_event(tenant_id: int, event_type: str, data: dict) -> None:
    db = SessionLocal()
    try:
        if not feature_flags.is_enabled(db, tenant_id, "workflow_engine"):
            return
        for wf in db.query(models.Workflow).filter(models.Workflow.tenant_id == tenant_id, models.Workflow.enabled == True,  # noqa: E712
                                                   models.Workflow.trigger_type == "event").all():
            cfg = json.loads(wf.trigger_config or "{}")
            if cfg.get("event_type") != event_type or not _filter_matches(cfg.get("filter"), data):
                continue
            try:
                payload = {"event": event_type, "data": data}
                run = create_run(db, wf, "event", payload)
                submit(run.id, payload)
            except Busy as e:
                record_rejected(db, wf, "event", f"Skipped: {e}")
    finally:
        db.close()


events.add_listener(dispatch_event)


def _scheduler_tick() -> None:
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        db.query(models.WorkflowRun).filter(models.WorkflowRun.status == "running", models.WorkflowRun.heartbeat_at < now - timedelta(seconds=STALE_AFTER_SECONDS)).update(
            {"status": "failed", "finished_at": now, "error": "The worker running this workflow was lost (a restart or crash) before it finished"}, synchronize_session=False)
        db.query(models.WorkflowRun).filter(models.WorkflowRun.status == "queued", models.WorkflowRun.created_at < now - timedelta(seconds=STALE_AFTER_SECONDS * 2)).update(
            {"status": "failed", "finished_at": now, "error": "The run was never picked up by a worker"}, synchronize_session=False)
        db.commit()

        due = db.query(models.Workflow).filter(models.Workflow.enabled == True, models.Workflow.trigger_type == "schedule",  # noqa: E712
                                               models.Workflow.next_run_at != None, models.Workflow.next_run_at <= now).limit(50).all()  # noqa: E711
        for wf in due:
            scheduled_for = wf.next_run_at
            cfg = json.loads(wf.trigger_config or "{}")
            # Compare-and-swap: with several replicas, exactly one wins this update and fires the schedule.
            won = db.query(models.Workflow).filter(models.Workflow.id == wf.id, models.Workflow.next_run_at == scheduled_for).update(
                {"next_run_at": next_fire(cfg, now)}, synchronize_session=False)
            db.commit()
            if not won or not feature_flags.is_enabled(db, wf.tenant_id, "workflow_engine"):
                continue
            db.refresh(wf)
            payload = {"scheduled_for": scheduled_for.isoformat()}
            try:
                run = create_run(db, wf, "schedule", payload)
                submit(run.id, payload)
            except Busy as e:
                record_rejected(db, wf, "schedule", f"Skipped: {e}")
    finally:
        db.close()


async def scheduler_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_scheduler_tick)
        except Exception:  # noqa: BLE001
            logger.exception("workflow scheduler tick failed")
        await asyncio.sleep(SCHEDULER_TICK_SECONDS)
