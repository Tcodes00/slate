"""
Workflow API: build, validate, enable, run, and inspect workflows (workflow_engine.py), manage the
credential vault they draw on, and receive signed webhooks.

Access: managers and above, and only while the `workflow_engine` feature flag is on for the workspace —
a workflow can call external URLs and send mail, so it's an explicit opt-in per workspace, not a
default. Everything is tenant-scoped; another workspace's workflow id is a 404, never a 403.

The public webhook receiver (POST /api/hooks/{webhook_id}) is the only unauthenticated door. The id in
the URL is 144 bits of randomness (unguessable) AND, by default, every delivery must be signed:
HMAC-SHA256 over "<timestamp>.<raw body>" with the workflow's own secret, timestamp within five minutes
— so a leaked URL alone is useless and a captured delivery can't be replayed later. Unknown, disabled,
and wrong-flag cases are all the same 404.
"""

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import models
import schemas
import workflow_engine as engine
from auth import require_role
from database import get_db
from feature_flags import is_enabled
from rate_limit import limiter
from security import decrypt_field, encrypt_field, log_audit

APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/")
MAX_DEFINITION_BYTES = 200_000
MAX_INPUT_BYTES = 100_000
MAX_WEBHOOK_BYTES = 1_000_000
SIGNATURE_TOLERANCE_SECONDS = 300

router = APIRouter(prefix="/api/workflows", tags=["workflows"])
hooks_router = APIRouter(prefix="/api/hooks", tags=["workflows"])
credentials_router = APIRouter(prefix="/api/workflow-credentials", tags=["workflows"])


def engine_user(db: Session = Depends(get_db), user: models.User = Depends(require_role("manager"))) -> models.User:
    if not is_enabled(db, user.tenant_id, "workflow_engine"):
        raise HTTPException(403, "The workflow engine is turned off for this workspace — an org admin can turn it on under Feature Flags")
    return user


def _get_wf(db: Session, tenant_id: int, wf_id: int) -> models.Workflow:
    wf = db.query(models.Workflow).filter(models.Workflow.id == wf_id, models.Workflow.tenant_id == tenant_id).first()
    if wf is None:
        raise HTTPException(404, "Workflow not found")
    return wf


def _check_definition(defn: dict) -> None:
    if len(json.dumps(defn)) > MAX_DEFINITION_BYTES:
        raise HTTPException(400, f"The definition is larger than {MAX_DEFINITION_BYTES // 1000} KB")
    errors = engine.validate_definition(defn)
    if errors:
        raise HTTPException(400, "Invalid workflow: " + " • ".join(errors[:6]) + (f" (+{len(errors) - 6} more)" if len(errors) > 6 else ""))


def _missing_credentials(db: Session, tenant_id: int, defn: dict) -> list:
    wanted = engine.referenced_credentials(defn)
    if not wanted:
        return []
    have = {c.name for c in db.query(models.WorkflowCredential).filter(models.WorkflowCredential.tenant_id == tenant_id)}
    return sorted(wanted - have)


def _summary(db: Session, wf: models.Workflow) -> dict:
    last = db.query(models.WorkflowRun).filter(models.WorkflowRun.workflow_id == wf.id).order_by(models.WorkflowRun.id.desc()).first()
    return {"id": wf.id, "name": wf.name, "description": wf.description or "", "trigger_type": wf.trigger_type, "enabled": wf.enabled,
            "version": wf.version, "last_run_at": wf.last_run_at.isoformat() if wf.last_run_at else None, "last_status": last.status if last else None,
            "next_run_at": wf.next_run_at.isoformat() if wf.next_run_at else None}


def _detail(db: Session, wf: models.Workflow) -> dict:
    out = _summary(db, wf)
    out["definition"] = json.loads(wf.definition)
    out["trigger_config"] = json.loads(wf.trigger_config or "{}")
    if wf.trigger_type == "webhook":
        out["webhook_url"] = f"{APP_BASE_URL}/api/hooks/{wf.webhook_id}"
    return out


def _run_out(db: Session, run: models.WorkflowRun, with_nodes: bool = True) -> dict:
    out = {"id": run.id, "workflow_id": run.workflow_id, "workflow_version": run.workflow_version, "status": run.status, "trigger_type": run.trigger_type,
           "error": run.error, "created_at": run.created_at.isoformat() if run.created_at else None,
           "started_at": run.started_at.isoformat() if run.started_at else None, "finished_at": run.finished_at.isoformat() if run.finished_at else None,
           "trigger_data": json.loads(run.trigger_data) if run.trigger_data else None, "output": json.loads(run.output) if run.output else None}
    if with_nodes:
        nodes = db.query(models.WorkflowNodeRun).filter(models.WorkflowNodeRun.run_id == run.id).order_by(models.WorkflowNodeRun.id).all()
        out["nodes"] = [{"node_id": n.node_id, "node_type": n.node_type, "status": n.status, "attempts": n.attempts, "duration_ms": n.duration_ms,
                         "started_at": n.started_at.isoformat() if n.started_at else None, "error": n.error,
                         "input": json.loads(n.input) if n.input else None, "output": json.loads(n.output) if n.output else None} for n in nodes]
    return out


async def _wait_for(db: Session, run_id: int, seconds: float = 30.0) -> models.WorkflowRun:
    deadline = time.monotonic() + seconds
    while True:
        db.expire_all()
        run = db.get(models.WorkflowRun, run_id)
        if run.status in ("succeeded", "failed") or time.monotonic() > deadline:
            return run
        await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# Catalog + validation (declared before /{wf_id} so those literal paths win)
# ---------------------------------------------------------------------------

@router.get("/node-types")
def node_types(user: models.User = Depends(engine_user)):
    return {"nodes": engine.NODE_CATALOG, "ops": list(engine.OPS), "limits": {
        "max_nodes": engine.MAX_NODES, "max_run_seconds": engine.MAX_RUN_SECONDS, "min_schedule_interval_seconds": engine.MIN_INTERVAL_SECONDS,
        "max_concurrent_runs": engine.MAX_CONCURRENT_PER_TENANT, "calls_per_run": engine.CALL_LIMITS}}


@router.post("/validate")
def validate(payload: schemas.WorkflowValidateRequest, user: models.User = Depends(engine_user)):
    errors = engine.validate_definition(payload.definition)
    return {"valid": not errors, "errors": errors}


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = Depends(get_db), user: models.User = Depends(engine_user)):
    run = db.query(models.WorkflowRun).filter(models.WorkflowRun.id == run_id, models.WorkflowRun.tenant_id == user.tenant_id).first()
    if run is None:
        raise HTTPException(404, "Run not found")
    return _run_out(db, run)


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------

@router.get("")
def list_workflows(db: Session = Depends(get_db), user: models.User = Depends(engine_user)):
    rows = db.query(models.Workflow).filter(models.Workflow.tenant_id == user.tenant_id).order_by(models.Workflow.id).all()
    return [_summary(db, wf) for wf in rows]


@router.post("")
def create_workflow(payload: schemas.WorkflowCreate, db: Session = Depends(get_db), user: models.User = Depends(engine_user)):
    name = payload.name.strip()
    if not name or len(name) > 80:
        raise HTTPException(400, "Name is required (80 characters at most)")
    _check_definition(payload.definition)
    kind, cfg = engine.trigger_from_definition(payload.definition)
    secret = secrets.token_urlsafe(32)
    wf = models.Workflow(
        tenant_id=user.tenant_id, name=name, description=payload.description.strip()[:500], definition=json.dumps(payload.definition),
        trigger_type=kind, trigger_config=json.dumps(cfg), enabled=False, version=1, webhook_id=secrets.token_urlsafe(18),
        webhook_secret_encrypted=encrypt_field(secret), created_by=user.id,
    )
    db.add(wf)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"A workflow named '{name}' already exists in this workspace")
    log_audit(db, user.tenant_id, user, "workflow.created", "workflow", wf.id, after={"name": name, "trigger": kind})
    db.commit()
    db.refresh(wf)
    out = _detail(db, wf)
    out["webhook_secret"] = secret  # the only time it's shown; rotate to get a new one
    return out


@router.get("/{wf_id}")
def get_workflow(wf_id: int, db: Session = Depends(get_db), user: models.User = Depends(engine_user)):
    return _detail(db, _get_wf(db, user.tenant_id, wf_id))


@router.put("/{wf_id}")
def update_workflow(wf_id: int, payload: schemas.WorkflowUpdate, db: Session = Depends(get_db), user: models.User = Depends(engine_user)):
    wf = _get_wf(db, user.tenant_id, wf_id)
    if payload.name is not None:
        if not payload.name.strip() or len(payload.name.strip()) > 80:
            raise HTTPException(400, "Name is required (80 characters at most)")
        wf.name = payload.name.strip()
    if payload.description is not None:
        wf.description = payload.description.strip()[:500]
    if payload.definition is not None:
        _check_definition(payload.definition)
        kind, cfg = engine.trigger_from_definition(payload.definition)
        wf.definition, wf.trigger_type, wf.trigger_config = json.dumps(payload.definition), kind, json.dumps(cfg)
        wf.version += 1
        if wf.enabled:
            missing = _missing_credentials(db, user.tenant_id, payload.definition)
            if missing:
                raise HTTPException(400, f"This enabled workflow would reference credentials that don't exist: {', '.join(missing)}")
            wf.next_run_at = engine.next_fire(cfg, datetime.utcnow()) if kind == "schedule" else None
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A workflow with that name already exists in this workspace")
    log_audit(db, user.tenant_id, user, "workflow.updated", "workflow", wf.id, after={"name": wf.name, "version": wf.version})
    db.commit()
    db.refresh(wf)
    return _detail(db, wf)


@router.patch("/{wf_id}/enabled")
def set_enabled(wf_id: int, payload: schemas.WorkflowEnabled, db: Session = Depends(get_db), user: models.User = Depends(engine_user)):
    wf = _get_wf(db, user.tenant_id, wf_id)
    if payload.enabled:
        defn = json.loads(wf.definition)
        _check_definition(defn)  # re-checked: limits (like the minimum schedule interval) can change between save and enable
        missing = _missing_credentials(db, user.tenant_id, defn)
        if missing:
            raise HTTPException(400, f"This workflow uses credentials that don't exist: {', '.join(missing)}")
        cfg = json.loads(wf.trigger_config or "{}")
        wf.enabled = True
        wf.next_run_at = engine.next_fire(cfg, datetime.utcnow()) if wf.trigger_type == "schedule" else None
    else:
        wf.enabled, wf.next_run_at = False, None
    log_audit(db, user.tenant_id, user, "workflow.enabled" if wf.enabled else "workflow.disabled", "workflow", wf.id, after={"name": wf.name})
    db.commit()
    db.refresh(wf)
    return _detail(db, wf)


@router.delete("/{wf_id}")
def delete_workflow(wf_id: int, db: Session = Depends(get_db), user: models.User = Depends(engine_user)):
    wf = _get_wf(db, user.tenant_id, wf_id)
    if db.query(models.WorkflowRun).filter(models.WorkflowRun.workflow_id == wf.id, models.WorkflowRun.status.in_(("queued", "running"))).first():
        raise HTTPException(409, "This workflow has a run in progress — wait for it to finish")
    run_ids = [r.id for r in db.query(models.WorkflowRun.id).filter(models.WorkflowRun.workflow_id == wf.id)]
    if run_ids:
        db.query(models.WorkflowNodeRun).filter(models.WorkflowNodeRun.run_id.in_(run_ids)).delete(synchronize_session=False)
        db.query(models.WorkflowRun).filter(models.WorkflowRun.workflow_id == wf.id).delete(synchronize_session=False)
    log_audit(db, user.tenant_id, user, "workflow.deleted", "workflow", wf.id, after={"name": wf.name})
    db.delete(wf)
    db.commit()
    return {"deleted": True}


@router.post("/{wf_id}/run")
async def run_workflow(wf_id: int, payload: schemas.WorkflowRunRequest, db: Session = Depends(get_db), user: models.User = Depends(engine_user)):
    """Manual run — allowed while the workflow is still disabled, so it can be tried before it's switched on."""
    wf = _get_wf(db, user.tenant_id, wf_id)
    _check_definition(json.loads(wf.definition))
    if len(json.dumps(payload.input)) > MAX_INPUT_BYTES:
        raise HTTPException(400, f"The input is larger than {MAX_INPUT_BYTES // 1000} KB")
    try:
        run = engine.create_run(db, wf, "manual", payload.input)
    except engine.Busy as e:
        raise HTTPException(429, str(e))
    log_audit(db, user.tenant_id, user, "workflow.run_started", "workflow", wf.id, after={"run_id": run.id})
    db.commit()
    engine.submit(run.id, payload.input)
    if payload.wait:
        run = await _wait_for(db, run.id)
    return _run_out(db, run)


@router.get("/{wf_id}/runs")
def list_runs(wf_id: int, status: Optional[str] = None, limit: int = 50, db: Session = Depends(get_db), user: models.User = Depends(engine_user)):
    wf = _get_wf(db, user.tenant_id, wf_id)
    q = db.query(models.WorkflowRun).filter(models.WorkflowRun.workflow_id == wf.id)
    if status:
        q = q.filter(models.WorkflowRun.status == status)
    return [_run_out(db, r, with_nodes=False) for r in q.order_by(models.WorkflowRun.id.desc()).limit(min(max(limit, 1), 200)).all()]


@router.post("/{wf_id}/rotate-webhook-secret")
def rotate_webhook_secret(wf_id: int, db: Session = Depends(get_db), user: models.User = Depends(engine_user)):
    wf = _get_wf(db, user.tenant_id, wf_id)
    secret = secrets.token_urlsafe(32)
    wf.webhook_secret_encrypted = encrypt_field(secret)
    log_audit(db, user.tenant_id, user, "workflow.webhook_secret_rotated", "workflow", wf.id, after={"name": wf.name})
    db.commit()
    return {"webhook_secret": secret, "webhook_url": f"{APP_BASE_URL}/api/hooks/{wf.webhook_id}"}


# ---------------------------------------------------------------------------
# Credential vault
# ---------------------------------------------------------------------------

_CRED_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")
_HOST = re.compile(r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_HEADER = re.compile(r"^[A-Za-z0-9-]{1,40}$")


def _cred_out(c: models.WorkflowCredential) -> dict:
    return {"id": c.id, "name": c.name, "kind": c.kind, "header_name": c.header_name, "username": c.username,
            "allowed_hosts": json.loads(c.allowed_hosts or "[]"), "created_at": c.created_at.isoformat() if c.created_at else None}


@credentials_router.get("")
def list_credentials(db: Session = Depends(get_db), user: models.User = Depends(engine_user)):
    return [_cred_out(c) for c in db.query(models.WorkflowCredential).filter(models.WorkflowCredential.tenant_id == user.tenant_id).order_by(models.WorkflowCredential.id)]


@credentials_router.post("")
def create_credential(payload: schemas.WorkflowCredentialCreate, db: Session = Depends(get_db), user: models.User = Depends(engine_user)):
    if not _CRED_NAME.match(payload.name):
        raise HTTPException(400, "Credential names are 1-60 letters, digits, '.', '_' or '-'")
    if payload.kind not in ("bearer", "header", "basic"):
        raise HTTPException(400, "Kind must be bearer, header, or basic")
    if not payload.secret or len(payload.secret) > 4000:
        raise HTTPException(400, "A secret is required (4000 characters at most)")
    if payload.kind == "header" and not (payload.header_name and _HEADER.match(payload.header_name)):
        raise HTTPException(400, "A header credential needs a header name like X-API-Key")
    if payload.kind == "basic" and not payload.username:
        raise HTTPException(400, "A basic credential needs a username")
    hosts = [h.strip().lower() for h in payload.allowed_hosts if h and h.strip()]
    if not hosts or any(not _HOST.match(h) or h == "*" for h in hosts):
        raise HTTPException(400, "List the host(s) this credential may be sent to, e.g. api.example.com or *.example.com — a credential with no host binding could be pointed at anyone's server")
    cred = models.WorkflowCredential(tenant_id=user.tenant_id, name=payload.name, kind=payload.kind, header_name=payload.header_name if payload.kind == "header" else None,
                                     username=payload.username if payload.kind == "basic" else None, secret_encrypted=encrypt_field(payload.secret), allowed_hosts=json.dumps(hosts))
    db.add(cred)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"A credential named '{payload.name}' already exists in this workspace")
    log_audit(db, user.tenant_id, user, "workflow_credential.created", "workflow_credential", cred.id, after={"name": cred.name, "kind": cred.kind, "allowed_hosts": hosts})
    db.commit()
    db.refresh(cred)
    return _cred_out(cred)


@credentials_router.delete("/{cred_id}")
def delete_credential(cred_id: int, db: Session = Depends(get_db), user: models.User = Depends(engine_user)):
    cred = db.query(models.WorkflowCredential).filter(models.WorkflowCredential.id == cred_id, models.WorkflowCredential.tenant_id == user.tenant_id).first()
    if cred is None:
        raise HTTPException(404, "Credential not found")
    for wf in db.query(models.Workflow).filter(models.Workflow.tenant_id == user.tenant_id):
        if cred.name in engine.referenced_credentials(json.loads(wf.definition)):
            raise HTTPException(409, f"Workflow '{wf.name}' uses this credential")
    log_audit(db, user.tenant_id, user, "workflow_credential.deleted", "workflow_credential", cred.id, after={"name": cred.name})
    db.delete(cred)
    db.commit()
    return {"deleted": True}


# ---------------------------------------------------------------------------
# Public, signed webhook receiver
# ---------------------------------------------------------------------------

def _valid_signature(secret: str, timestamp: str, body: bytes, header: str) -> bool:
    if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > SIGNATURE_TOLERANCE_SECONDS:
        return False
    expected = "v1=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), (header or "").encode())


@hooks_router.post("/{webhook_id}")
@limiter.limit("120/minute")
async def receive_webhook(webhook_id: str, request: Request, wait: bool = False, db: Session = Depends(get_db)):
    wf = db.query(models.Workflow).filter(models.Workflow.webhook_id == webhook_id).first()
    if wf is None or not wf.enabled or wf.trigger_type != "webhook" or not is_enabled(db, wf.tenant_id, "workflow_engine"):
        raise HTTPException(404, "Not found")
    raw = await request.body()
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise HTTPException(413, "Payload too large")
    cfg = json.loads(wf.trigger_config or "{}")
    if cfg.get("require_signature", True):
        secret = decrypt_field(wf.webhook_secret_encrypted)
        if not _valid_signature(secret, request.headers.get("x-slate-timestamp", ""), raw, request.headers.get("x-slate-signature", "")):
            raise HTTPException(401, "Missing, invalid, or expired signature")
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        raise HTTPException(400, "The body must be JSON")
    if not isinstance(data, dict):
        raise HTTPException(400, "The JSON body must be an object")
    try:
        run = engine.create_run(db, wf, "webhook", data)
    except engine.Busy as e:
        raise HTTPException(429, str(e))
    engine.submit(run.id, data)
    if wait:
        run = await _wait_for(db, run.id)
        return {"run_id": run.id, "status": run.status, "error": run.error, "output": json.loads(run.output) if run.output else None}
    return JSONResponse({"run_id": run.id, "status": "queued"}, status_code=202)
