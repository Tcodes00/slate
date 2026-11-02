from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db

router = APIRouter(prefix="/api/automation", tags=["automation"])


def _rule_out(r: models.AutomationRule) -> dict:
    return {
        "id": r.id, "name": r.name, "trigger_stage": r.trigger_stage,
        "action_type": r.action_type, "action_note": r.action_note,
        "enabled": r.enabled == "true", "run_count": r.run_count,
    }


@router.get("/rules", response_model=list[schemas.RuleOut])
def list_rules(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    rules = db.query(models.AutomationRule).filter(models.AutomationRule.tenant_id == user.tenant_id).all()
    return [_rule_out(r) for r in rules]


@router.post("/rules", response_model=schemas.RuleOut)
def create_rule(payload: schemas.RuleCreate, db: Session = Depends(get_db), user: models.User = Depends(require_role("manager"))):
    rule = models.AutomationRule(
        tenant_id=user.tenant_id, name=payload.name, trigger_stage=payload.trigger_stage,
        action_type=payload.action_type, action_note=payload.action_note,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return _rule_out(rule)


@router.patch("/rules/{rule_id}", response_model=schemas.RuleOut)
def toggle_rule(rule_id: int, payload: schemas.RuleUpdate, db: Session = Depends(get_db), user: models.User = Depends(require_role("manager"))):
    rule = db.query(models.AutomationRule).filter(models.AutomationRule.id == rule_id, models.AutomationRule.tenant_id == user.tenant_id).first()
    if not rule:
        raise HTTPException(404, "Rule not found")
    rule.enabled = "true" if payload.enabled else "false"
    db.commit()
    db.refresh(rule)
    return _rule_out(rule)
