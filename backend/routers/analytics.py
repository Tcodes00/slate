from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import models
from auth import get_current_user
from cache import cache_get, cache_set
from database import get_db

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

STAGES = ["Lead", "Qualified", "Proposal", "Won"]


@router.get("/funnel")
def funnel(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """How many distinct deals have ever reached each stage — computed from
    real stage-transition history, not the current snapshot. Cached for 30s
    behind a real Redis instance; falls through to direct computation if
    Redis isn't running."""
    cache_key = f"analytics:funnel:{user.tenant_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    history = (
        db.query(models.DealStageHistory)
        .filter(models.DealStageHistory.tenant_id == user.tenant_id)
        .all()
    )
    reached = defaultdict(set)
    for h in history:
        reached[h.to_stage].add(h.deal_id)

    result = [{"stage": s, "deals_reached": len(reached.get(s, set()))} for s in STAGES]
    cache_set(cache_key, result)
    return result


@router.get("/win-rate")
def win_rate(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    cache_key = f"analytics:win-rate:{user.tenant_id}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    deals = db.query(models.Deal).filter(models.Deal.tenant_id == user.tenant_id).all()
    won = sum(1 for d in deals if d.stage == "Won")
    lost = sum(1 for d in deals if d.stage == "Lost")
    closed = won + lost
    result = {
        "won": won,
        "lost": lost,
        "win_rate_pct": round(100 * won / closed, 1) if closed else None,
    }
    cache_set(cache_key, result)
    return result


@router.get("/cycle-time")
def cycle_time(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Average days from deal creation to reaching Won, computed from the
    real creation timestamp and the real 'entered Won' history event —
    not the deal's updated_at, which could move for unrelated edits."""
    won_deals = (
        db.query(models.Deal)
        .filter(models.Deal.tenant_id == user.tenant_id, models.Deal.stage == "Won")
        .all()
    )
    if not won_deals:
        return {"avg_days_to_win": None, "sample_size": 0}

    durations = []
    for deal in won_deals:
        won_event = (
            db.query(models.DealStageHistory)
            .filter(
                models.DealStageHistory.deal_id == deal.id,
                models.DealStageHistory.to_stage == "Won",
            )
            .order_by(models.DealStageHistory.changed_at.desc())
            .first()
        )
        if won_event:
            durations.append((won_event.changed_at - deal.created_at).total_seconds() / 86400)

    if not durations:
        return {"avg_days_to_win": None, "sample_size": 0}

    return {"avg_days_to_win": round(sum(durations) / len(durations), 1), "sample_size": len(durations)}


@router.get("/revenue")
def revenue(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Realized revenue by month, from actually-paid invoices — not
    projected/booked value, actual cash collected."""
    paid = (
        db.query(models.Invoice)
        .filter(models.Invoice.tenant_id == user.tenant_id, models.Invoice.status == "Paid")
        .all()
    )
    by_month = defaultdict(float)
    for inv in paid:
        if inv.paid_at:
            key = inv.paid_at.strftime("%Y-%m")
            by_month[key] += inv.amount

    return [{"month": k, "revenue": v} for k, v in sorted(by_month.items())]
