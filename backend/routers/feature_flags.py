from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db
from feature_flags import REGISTRY, get_all_flags, set_flag
from security import log_audit

router = APIRouter(prefix="/api/feature-flags", tags=["feature-flags"])


@router.get("", response_model=list[schemas.FeatureFlagOut])
def list_flags(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Open to every authenticated role, not just org_admin — the
    frontend needs to know what's on to gate its own UI everywhere, not
    just on this settings screen. Only the PATCH below is admin-only."""
    current = get_all_flags(db, user.tenant_id)
    return [
        {"key": key, "label": label, "description": description, "enabled": current[key]}
        for key, (label, description, _) in REGISTRY.items()
    ]


@router.patch("/{key}", response_model=schemas.FeatureFlagOut)
def update_flag(
    key: str,
    payload: schemas.FeatureFlagUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("org_admin")),
):
    before = get_all_flags(db, user.tenant_id).get(key)
    row = set_flag(db, user.tenant_id, key, payload.enabled, user)
    log_audit(
        db, user.tenant_id, user, "feature_flag.updated", "feature_flag", None,
        before={"key": key, "enabled": before}, after={"key": key, "enabled": row.enabled},
    )
    db.commit()
    label, description, _ = REGISTRY[key]
    return {"key": key, "label": label, "description": description, "enabled": row.enabled}
