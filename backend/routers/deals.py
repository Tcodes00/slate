from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from cache import cache_invalidate
from database import get_db
from events import publish_event

router = APIRouter(prefix="/api/deals", tags=["deals"])


def _deal_out(deal: models.Deal) -> dict:
    last_activity = deal.updated_at
    if deal.activities:
        latest = max(a.occurred_at for a in deal.activities)
        if latest > last_activity:
            last_activity = latest
    return {
        "id": deal.id,
        "name": deal.name,
        "company": deal.party.display_name if deal.party else "",
        "contact_email": deal.party.primary_email if deal.party else None,
        "value": deal.value,
        "stage": deal.stage,
        "created_at": deal.created_at,
        "last_activity_at": last_activity,
    }


def _get_owned_deal(deal_id: int, db: Session, user: models.User) -> models.Deal:
    # Every lookup is scoped to the caller's tenant — this is the actual
    # enforcement point for multi-tenancy, not just a UI-level filter.
    deal = (
        db.query(models.Deal)
        .filter(models.Deal.id == deal_id, models.Deal.tenant_id == user.tenant_id)
        .first()
    )
    if not deal:
        raise HTTPException(404, "Deal not found")
    return deal


def _run_automation_rules(db: Session, tenant_id: int, deal: models.Deal, new_stage: str) -> None:
    """The real trigger firing — not just a config screen. Matching rules
    execute their action and increment run_count, so Automation Studio's
    numbers reflect actual executions."""
    rules = (
        db.query(models.AutomationRule)
        .filter(
            models.AutomationRule.tenant_id == tenant_id,
            models.AutomationRule.trigger_stage == new_stage,
            models.AutomationRule.enabled == "true",
        )
        .all()
    )
    for rule in rules:
        if rule.action_type == "create_task":
            project = db.query(models.Project).filter(models.Project.tenant_id == tenant_id).first()
            if project:
                db.add(models.Task(
                    tenant_id=tenant_id, project_id=project.id,
                    title=rule.action_note or f"Follow up: {deal.name}",
                ))
        rule.run_count += 1
        publish_event(tenant_id, "automation.fired", {
            "rule_id": rule.id, "rule_name": rule.name, "deal_name": deal.name,
            "trigger_stage": new_stage, "run_count": rule.run_count,
        })


@router.get("", response_model=list[schemas.DealOut])
def list_deals(
    stage: Optional[str] = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.Deal).filter(models.Deal.tenant_id == user.tenant_id)
    if stage:
        q = q.filter(models.Deal.stage == stage)
    deals = q.order_by(models.Deal.created_at.desc()).all()
    return [_deal_out(d) for d in deals]


@router.post("", response_model=schemas.DealOut)
def create_deal(
    payload: schemas.DealCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    party = models.Party(
        tenant_id=user.tenant_id,
        party_type="organization",
        display_name=payload.company,
        owner_user_id=user.id,
    )
    db.add(party)
    db.flush()

    deal = models.Deal(
        tenant_id=user.tenant_id,
        party_id=party.id,
        name=payload.name,
        value=payload.value,
        stage=payload.stage,
        owner_user_id=user.id,
    )
    db.add(deal)
    db.flush()
    db.add(models.DealStageHistory(
        tenant_id=user.tenant_id, deal_id=deal.id, from_stage=None, to_stage=deal.stage,
    ))
    db.commit()
    db.refresh(deal)
    return _deal_out(deal)


@router.patch("/{deal_id}", response_model=schemas.DealOut)
def update_deal(
    deal_id: int,
    payload: schemas.DealUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    deal = _get_owned_deal(deal_id, db, user)
    if payload.name is not None:
        deal.name = payload.name
    if payload.stage is not None and payload.stage != deal.stage:
        db.add(models.DealStageHistory(
            tenant_id=user.tenant_id, deal_id=deal.id, from_stage=deal.stage, to_stage=payload.stage,
        ))
        deal.stage = payload.stage
        _run_automation_rules(db, user.tenant_id, deal, payload.stage)
        cache_invalidate(f"analytics:funnel:{user.tenant_id}")
        cache_invalidate(f"analytics:win-rate:{user.tenant_id}")
        publish_event(user.tenant_id, "deal.updated", {
            "id": deal.id, "name": deal.name, "stage": deal.stage,
            "company": deal.party.display_name if deal.party else None,
        })
    if payload.value is not None:
        deal.value = payload.value
    deal.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(deal)
    return _deal_out(deal)


@router.delete("/{deal_id}")
def delete_deal(
    deal_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("manager")),
):
    deal = _get_owned_deal(deal_id, db, user)
    db.delete(deal)
    db.commit()
    return {"ok": True}


@router.get("/{deal_id}/activities", response_model=list[schemas.ActivityOut])
def list_activities(
    deal_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    _get_owned_deal(deal_id, db, user)
    activities = (
        db.query(models.Activity)
        .filter(models.Activity.deal_id == deal_id, models.Activity.tenant_id == user.tenant_id)
        .order_by(models.Activity.occurred_at.desc())
        .all()
    )
    return activities


@router.post("/{deal_id}/activities", response_model=schemas.ActivityOut)
def add_activity(
    deal_id: int,
    payload: schemas.ActivityCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    _get_owned_deal(deal_id, db, user)
    activity = models.Activity(tenant_id=user.tenant_id, deal_id=deal_id, content=payload.content)
    db.add(activity)
    db.commit()
    db.refresh(activity)
    return activity
