import hashlib
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db

router = APIRouter(prefix="/api/keys", tags=["api-manager"])


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _key_out(k: models.ApiKey) -> dict:
    return {
        "id": k.id, "name": k.name, "key_prefix": k.key_prefix,
        "is_self_serve": k.is_self_serve == "true", "revoked": k.revoked == "true",
        "usage_count": k.usage_count, "created_at": k.created_at, "last_used_at": k.last_used_at,
    }


def _issue_key(db: Session, tenant_id: int, user_id: int, name: str, self_serve: bool) -> dict:
    raw = "sk_slate_" + secrets.token_urlsafe(24)
    key = models.ApiKey(
        tenant_id=tenant_id, name=name, key_prefix=raw[:16] + "…",
        hashed_key=_hash_key(raw), created_by_user_id=user_id,
        is_self_serve="true" if self_serve else "false",
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return {"id": key.id, "name": key.name, "key_prefix": key.key_prefix, "full_key": raw}


@router.get("", response_model=list[schemas.ApiKeyOut])
def list_keys(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    keys = db.query(models.ApiKey).filter(models.ApiKey.tenant_id == user.tenant_id).order_by(models.ApiKey.created_at.desc()).all()
    return [_key_out(k) for k in keys]


@router.post("", response_model=schemas.ApiKeyCreatedOut)
def create_key(payload: schemas.ApiKeyCreate, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    """Admin-issued key — the API Manager flow."""
    return _issue_key(db, user.tenant_id, user.id, payload.name, self_serve=False)


@router.post("/self-serve", response_model=schemas.ApiKeyCreatedOut)
def create_self_serve_key(payload: schemas.ApiKeyCreate, db: Session = Depends(get_db), user: models.User = Depends(require_role("standard_user"))):
    """Same underlying mechanism, lower privilege bar — the Developer
    Portal flow. Real self-serve registration, not a mockup form."""
    return _issue_key(db, user.tenant_id, user.id, payload.name, self_serve=True)


@router.post("/{key_id}/revoke", response_model=schemas.ApiKeyOut)
def revoke_key(key_id: int, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    key = db.query(models.ApiKey).filter(models.ApiKey.id == key_id, models.ApiKey.tenant_id == user.tenant_id).first()
    if not key:
        raise HTTPException(404, "Key not found")
    key.revoked = "true"
    db.commit()
    db.refresh(key)
    return _key_out(key)


@router.post("/{key_id}/simulate-use", response_model=schemas.ApiKeyOut)
def simulate_use(key_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Increments real usage stats — stands in for what a real external
    API call authenticating with this key would do to these same columns."""
    key = db.query(models.ApiKey).filter(models.ApiKey.id == key_id, models.ApiKey.tenant_id == user.tenant_id).first()
    if not key:
        raise HTTPException(404, "Key not found")
    if key.revoked == "true":
        raise HTTPException(401, "This key has been revoked")
    key.usage_count += 1
    key.last_used_at = datetime.utcnow()
    db.commit()
    db.refresh(key)
    return _key_out(key)
