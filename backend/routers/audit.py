from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import models
from auth import require_role
from database import get_db

router = APIRouter(prefix="/api/audit-log", tags=["audit"])


@router.get("")
def list_audit_log(
    limit: int = 100,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("org_admin")),
):
    entries = (
        db.query(models.AuditLog)
        .filter(models.AuditLog.tenant_id == user.tenant_id)
        .order_by(models.AuditLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": e.id, "actor_name": e.actor_name, "action": e.action,
            "resource_type": e.resource_type, "resource_id": e.resource_id,
            "before": e.before, "after": e.after, "created_at": e.created_at,
        }
        for e in entries
    ]
