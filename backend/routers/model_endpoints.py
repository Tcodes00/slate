"""
Custom and local model endpoints: an org admin points the workspace at
their own OpenAI-compatible model server (self-hosted vLLM/Ollama/LM
Studio/llama.cpp, a LiteLLM or OpenRouter-style gateway, a fine-tuned
model behind any of them) and then selects it in AI Studio like any other
provider. Org admins only: these rows hold a URL the server will call and,
usually, a key.

Two properties worth stating, both enforced in code and checked by
backend/scripts/verify_llm_endpoints.py:

  * The key is write-only. It's encrypted at rest with the same
    field-level encryption as the LDAP bind password and OAuth client
    secret, and no response ever contains it — reads say has_api_key.
  * The URL is untrusted. Every call (and the save-time check) goes
    through outbound.py: resolved, checked, connection pinned, no
    redirects, bounded response. A workspace can never use this to reach
    another workspace's endpoint either — ownership is re-checked against
    the *calling* tenant on every read (routers/ai_studio.py).
"""

import re
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import ai_providers
import models
import outbound
import schemas
from auth import require_role
from database import SessionLocal, get_db
from security import decrypt_field, encrypt_field, log_audit

router = APIRouter(prefix="/api/ai-studio/endpoints", tags=["ai-studio"])

KINDS = ("chat", "embedding")
_HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")


def resolve_endpoint(tenant_id: int, endpoint_id: int):
    """Registered with ai_providers below. Looks up by BOTH ids, so an endpoint can only ever
    be resolved on behalf of the workspace that owns it."""
    db = SessionLocal()
    try:
        endpoint = db.query(models.CustomModelEndpoint).filter(
            models.CustomModelEndpoint.id == endpoint_id, models.CustomModelEndpoint.tenant_id == tenant_id,
            models.CustomModelEndpoint.kind == "chat", models.CustomModelEndpoint.enabled == True,  # noqa: E712
        ).first()
        if endpoint is None:
            return None
        return {
            "name": endpoint.name, "base_url": endpoint.base_url, "model": endpoint.model, "auth_header": endpoint.auth_header,
            "api_key": decrypt_field(endpoint.api_key_encrypted) if endpoint.api_key_encrypted else "",
        }
    finally:
        db.close()


ai_providers.set_endpoint_resolver(resolve_endpoint)


def _out(e: models.CustomModelEndpoint) -> dict:
    return {
        "id": e.id, "name": e.name, "kind": e.kind, "base_url": e.base_url, "model": e.model,
        "has_api_key": bool(e.api_key_encrypted), "auth_header": e.auth_header, "enabled": e.enabled,
    }


def _get(db: Session, tenant_id: int, endpoint_id: int) -> models.CustomModelEndpoint:
    endpoint = db.query(models.CustomModelEndpoint).filter(
        models.CustomModelEndpoint.id == endpoint_id, models.CustomModelEndpoint.tenant_id == tenant_id
    ).first()
    if endpoint is None:
        raise HTTPException(404, "Endpoint not found")
    return endpoint


def _is_active_provider(db: Session, tenant_id: int, endpoint_id: int) -> bool:
    return db.query(models.AIProviderConfig).filter(
        models.AIProviderConfig.tenant_id == tenant_id, models.AIProviderConfig.provider == f"custom:{endpoint_id}"
    ).first() is not None


@router.get("", response_model=list[schemas.ModelEndpointOut])
def list_endpoints(db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    rows = db.query(models.CustomModelEndpoint).filter(models.CustomModelEndpoint.tenant_id == user.tenant_id).order_by(models.CustomModelEndpoint.id).all()
    return [_out(e) for e in rows]


@router.post("", response_model=schemas.ModelEndpointOut)
async def create_endpoint(payload: schemas.ModelEndpointCreate, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    name, model = payload.name.strip(), payload.model.strip()
    if not name or len(name) > 60:
        raise HTTPException(400, "Name is required (60 characters at most)")
    if payload.kind not in KINDS:
        raise HTTPException(400, f"Kind must be one of: {', '.join(KINDS)}")
    if not model:
        raise HTTPException(400, "Model name is required")
    if not _HEADER_RE.match(payload.auth_header):
        raise HTTPException(400, "Auth header must be a plain header name like Authorization or api-key")
    try:
        base_url = await outbound.check_url(payload.base_url, "Base URL")
    except outbound.OutboundError as e:
        raise HTTPException(400, str(e))

    endpoint = models.CustomModelEndpoint(
        tenant_id=user.tenant_id, name=name, kind=payload.kind, base_url=base_url, model=model, auth_header=payload.auth_header,
        api_key_encrypted=encrypt_field(payload.api_key) if payload.api_key else None,
    )
    db.add(endpoint)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"An endpoint named '{name}' already exists in this workspace")
    log_audit(db, user.tenant_id, user, "model_endpoint.created", "model_endpoint", endpoint.id,
              after={"name": name, "kind": payload.kind, "base_url": base_url, "model": model})
    db.commit()
    db.refresh(endpoint)
    return _out(endpoint)


@router.patch("/{endpoint_id}", response_model=schemas.ModelEndpointOut)
async def update_endpoint(endpoint_id: int, payload: schemas.ModelEndpointUpdate, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    endpoint = _get(db, user.tenant_id, endpoint_id)
    if payload.enabled is False and _is_active_provider(db, user.tenant_id, endpoint.id):
        raise HTTPException(409, "This endpoint is the workspace's active AI provider — switch to another provider in AI Studio first")
    if payload.name is not None:
        if not payload.name.strip() or len(payload.name.strip()) > 60:
            raise HTTPException(400, "Name is required (60 characters at most)")
        endpoint.name = payload.name.strip()
    if payload.model is not None:
        if not payload.model.strip():
            raise HTTPException(400, "Model name is required")
        endpoint.model = payload.model.strip()
    if payload.base_url is not None:
        try:
            endpoint.base_url = await outbound.check_url(payload.base_url, "Base URL")
        except outbound.OutboundError as e:
            raise HTTPException(400, str(e))
    if payload.api_key:
        endpoint.api_key_encrypted = encrypt_field(payload.api_key)
    if payload.enabled is not None:
        endpoint.enabled = payload.enabled
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "An endpoint with that name already exists in this workspace")
    log_audit(db, user.tenant_id, user, "model_endpoint.updated", "model_endpoint", endpoint.id,
              after={"name": endpoint.name, "base_url": endpoint.base_url, "model": endpoint.model, "enabled": endpoint.enabled, "key_rotated": bool(payload.api_key)})
    db.commit()
    db.refresh(endpoint)
    return _out(endpoint)


@router.delete("/{endpoint_id}")
def delete_endpoint(endpoint_id: int, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    endpoint = _get(db, user.tenant_id, endpoint_id)
    if _is_active_provider(db, user.tenant_id, endpoint.id):
        raise HTTPException(409, "This endpoint is the workspace's active AI provider — switch to another provider in AI Studio first")
    log_audit(db, user.tenant_id, user, "model_endpoint.deleted", "model_endpoint", endpoint.id, after={"name": endpoint.name})
    db.delete(endpoint)
    db.commit()
    return {"deleted": True}


@router.post("/{endpoint_id}/test")
async def test_endpoint(endpoint_id: int, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    """A real round trip through the exact code path Copilot and agents use. Always HTTP 200: a
    failing endpoint is an answer to 'does this work', not a server error."""
    endpoint = _get(db, user.tenant_id, endpoint_id)
    if endpoint.kind != "chat":
        return {"ok": False, "error": "Only chat endpoints are tested here; embedding endpoints are exercised when the knowledge base is indexed"}
    started = time.monotonic()
    try:
        reply = await ai_providers.call_llm(f"custom:{user.tenant_id}:{endpoint.id}", endpoint.model, [{"role": "user", "content": "Reply with the single word: pong"}], 16)
        return {"ok": True, "latency_ms": int((time.monotonic() - started) * 1000), "reply": reply[:120]}
    except RuntimeError as e:
        return {"ok": False, "latency_ms": int((time.monotonic() - started) * 1000), "error": str(e)[:300]}
