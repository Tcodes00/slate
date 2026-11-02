"""
Compliance Center API (compliance.py): data-subject export and erasure, legal holds, retention policies, the
consent ledger, access reviews, a breach register with the 72-hour clock, live control status mapped to SOC 2 /
ISO 27001 / GDPR, a tamper-evident evidence pack, and instant user suspension.

Organization admins only, except the consent ledger (managers can record consent). Every mutating action writes
an audit entry. Nothing here ever returns a secret, and the record of an erasure stores a hash and a masked form of
the email — not the email.
"""

import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

import compliance
import models
import schemas
from auth import ROLE_RANK, require_role
from database import get_db
from security import log_audit

router = APIRouter(prefix="/api/compliance", tags=["compliance"])
ROLES = tuple(ROLE_RANK)


def _tenant(db: Session, user: models.User) -> models.Tenant:
    return db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()


def _active_admin_ids(db: Session, tenant_id: int) -> list:
    locked = {r[0] for r in db.query(models.UserLock.user_id).filter(models.UserLock.tenant_id == tenant_id)}
    return [u.id for u in db.query(models.User).filter(models.User.tenant_id == tenant_id, models.User.role == "org_admin") if u.id not in locked]


# ---------------------------------------------------------------------------
# Controls and evidence
# ---------------------------------------------------------------------------

@router.get("/controls")
def controls(db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    items = compliance.compute_controls(db, _tenant(db, user))
    return {"controls": items, "summary": compliance.summarise(items)}


@router.post("/controls/{control_id}/attest")
def attest(control_id: str, payload: schemas.AttestIn, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    if control_id not in compliance.MANUAL_CONTROLS:
        raise HTTPException(400, "Only manual controls can be attested — measured controls are computed, and can't be declared passing")
    if len(payload.note.strip()) < 10:
        raise HTTPException(400, "Say what was checked and when (at least 10 characters) — an attestation with no substance is worth nothing to an auditor")
    row = models.ControlAttestation(tenant_id=user.tenant_id, control_id=control_id, note=payload.note.strip()[:1000], attested_by=user.id)
    db.add(row)
    log_audit(db, user.tenant_id, user, "compliance.control_attested", "control", None, after={"control": control_id})
    db.commit()
    return {"control_id": control_id, "attested": True}


@router.get("/evidence-pack")
def evidence(db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    pack = compliance.evidence_pack(db, _tenant(db, user))
    log_audit(db, user.tenant_id, user, "compliance.evidence_pack_generated", "evidence_pack", None, after={"sha256": pack["integrity_sha256"]})
    db.commit()
    return JSONResponse(pack, headers={"Content-Disposition": 'attachment; filename="slate-evidence-pack.json"', "Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Data-subject requests
# ---------------------------------------------------------------------------

def _record_dsar(db: Session, user: models.User, kind: str, email: str, status: str, summary: dict) -> models.DataSubjectRequest:
    now = datetime.utcnow()
    row = models.DataSubjectRequest(tenant_id=user.tenant_id, kind=kind, subject_hash=compliance.email_hash(email), subject_masked=compliance.mask_email(email), status=status,
                                    requested_by=user.id, summary=json.dumps(summary), created_at=now, completed_at=now if status == "completed" else None)
    db.add(row)
    return row


@router.post("/dsar/export")
def dsar_export(payload: schemas.DsarRequest, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    try:
        data = compliance.export_subject(db, user.tenant_id, payload.email)
    except compliance.ComplianceError as e:
        _record_dsar(db, user, "export", payload.email, "refused", {"reason": str(e)})
        db.commit()
        raise HTTPException(e.status, str(e))
    row = _record_dsar(db, user, "export", payload.email, "completed", {"tables": {k: len(v) for k, v in data["tables"].items()}, "other_mentions": len(data["other_mentions"])})
    db.flush()
    log_audit(db, user.tenant_id, user, "compliance.dsar_export", "dsar", row.id, after={"subject": compliance.mask_email(payload.email)})
    db.commit()
    return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="dsar-export-{row.id}.json"', "Cache-Control": "no-store"})


@router.post("/dsar/erase")
def dsar_erase(payload: schemas.DsarRequest, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    if payload.confirm is not True:
        raise HTTPException(400, "Erasure is irreversible — send confirm: true")
    try:
        report = compliance.erase_subject(db, user.tenant_id, user, payload.email)
    except compliance.ComplianceError as e:
        db.rollback()
        _record_dsar(db, user, "erasure", payload.email, "refused", {"reason": str(e)})
        db.commit()
        raise HTTPException(e.status, str(e))
    row = _record_dsar(db, user, "erasure", payload.email, "completed", {k: report[k] for k in ("user_anonymised", "contacts_anonymised", "passkeys_removed", "audit_entries_pseudonymised", "clean")}
                       | {"residual_email_mentions": len(report["residual_email_mentions"]), "needs_review": len(report["needs_review"])})
    db.flush()
    log_audit(db, user.tenant_id, user, "compliance.dsar_erasure", "dsar", row.id, after={"subject": compliance.mask_email(payload.email), "clean": report["clean"]})
    db.commit()
    return {"request_id": row.id, **report}


@router.get("/dsar")
def dsar_list(db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    rows = db.query(models.DataSubjectRequest).filter(models.DataSubjectRequest.tenant_id == user.tenant_id).order_by(models.DataSubjectRequest.id.desc()).all()
    return [{"id": r.id, "kind": r.kind, "subject": r.subject_masked, "status": r.status, "created_at": r.created_at.isoformat(), "summary": json.loads(r.summary or "{}")} for r in rows]


# ---------------------------------------------------------------------------
# Legal holds
# ---------------------------------------------------------------------------

@router.get("/legal-holds")
def holds(db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    rows = db.query(models.LegalHold).filter(models.LegalHold.tenant_id == user.tenant_id).order_by(models.LegalHold.id).all()
    return [{"id": h.id, "email": h.subject_email, "reason": h.reason, "created_at": h.created_at.isoformat(), "released_at": h.released_at.isoformat() if h.released_at else None} for h in rows]


@router.post("/legal-holds")
def place_hold(payload: schemas.LegalHoldIn, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    if not payload.reason.strip():
        raise HTTPException(400, "A reason is required")
    hold = models.LegalHold(tenant_id=user.tenant_id, subject_email=payload.email.strip().lower(), reason=payload.reason.strip()[:500], created_by=user.id)
    db.add(hold)
    db.flush()
    log_audit(db, user.tenant_id, user, "compliance.legal_hold_placed", "legal_hold", hold.id, after={"subject": compliance.mask_email(payload.email)})
    db.commit()
    return {"id": hold.id}


@router.delete("/legal-holds/{hold_id}")
def release_hold(hold_id: int, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    hold = db.query(models.LegalHold).filter(models.LegalHold.id == hold_id, models.LegalHold.tenant_id == user.tenant_id).first()
    if hold is None:
        raise HTTPException(404, "Hold not found")
    hold.released_at = datetime.utcnow()
    log_audit(db, user.tenant_id, user, "compliance.legal_hold_released", "legal_hold", hold.id, after={})
    db.commit()
    return {"released": True}


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

@router.get("/retention")
def get_retention(db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    rows = {p.entity: p for p in db.query(models.RetentionPolicy).filter(models.RetentionPolicy.tenant_id == user.tenant_id)}
    return [{"entity": e, "label": meta["label"], "min_days": meta["min_days"], "days": rows[e].days if e in rows else None, "enabled": rows[e].enabled if e in rows else False,
             "last_run_at": rows[e].last_run_at.isoformat() if e in rows and rows[e].last_run_at else None, "last_deleted": rows[e].last_deleted if e in rows else None}
            for e, meta in compliance.RETENTION_ENTITIES.items()]


@router.put("/retention")
def set_retention(payload: schemas.RetentionIn, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    meta = compliance.RETENTION_ENTITIES.get(payload.entity)
    if meta is None:
        raise HTTPException(400, f"Entity must be one of: {', '.join(compliance.RETENTION_ENTITIES)}")
    if not meta["min_days"] <= payload.days <= 3650:
        raise HTTPException(400, f"Retention for {meta['label'].lower()} must be between {meta['min_days']} and 3650 days"
                            + (" — the audit log has a floor so a policy can't be used to erase the evidence of its own purge" if payload.entity == "audit_log" else ""))
    policy = db.query(models.RetentionPolicy).filter(models.RetentionPolicy.tenant_id == user.tenant_id, models.RetentionPolicy.entity == payload.entity).first()
    if policy is None:
        policy = models.RetentionPolicy(tenant_id=user.tenant_id, entity=payload.entity, days=payload.days, enabled=payload.enabled)
        db.add(policy)
    else:
        policy.days, policy.enabled = payload.days, payload.enabled
    log_audit(db, user.tenant_id, user, "compliance.retention_set", "retention_policy", None, after={"entity": payload.entity, "days": payload.days, "enabled": payload.enabled})
    db.commit()
    return {"entity": payload.entity, "days": payload.days, "enabled": payload.enabled}


@router.post("/retention/run")
def run_retention_now(db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    return {"deleted": compliance.run_retention(db, user.tenant_id)}


# ---------------------------------------------------------------------------
# Consent ledger
# ---------------------------------------------------------------------------

@router.post("/consent")
def record_consent(payload: schemas.ConsentIn, db: Session = Depends(get_db), user: models.User = Depends(require_role("manager"))):
    if not payload.purpose.strip() or "@" not in payload.email:
        raise HTTPException(400, "A valid email and a purpose are required")
    row = models.ConsentRecord(tenant_id=user.tenant_id, subject_email=payload.email.strip().lower(), purpose=payload.purpose.strip()[:100], granted=payload.granted,
                               source=(payload.source or "")[:200] or None, recorded_by=user.id)
    db.add(row)
    db.commit()
    return {"id": row.id, "granted": row.granted}


@router.get("/consent")
def consent_history(email: str, db: Session = Depends(get_db), user: models.User = Depends(require_role("manager"))):
    rows = db.query(models.ConsentRecord).filter(models.ConsentRecord.tenant_id == user.tenant_id, func.lower(models.ConsentRecord.subject_email) == email.strip().lower()).order_by(models.ConsentRecord.id).all()
    current: dict = {}
    for r in rows:
        current[r.purpose] = r.granted  # append-only: the latest row per purpose is the current state
    return {"email": email, "current": current, "history": [{"purpose": r.purpose, "granted": r.granted, "source": r.source, "at": r.created_at.isoformat()} for r in rows]}


# ---------------------------------------------------------------------------
# Access reviews
# ---------------------------------------------------------------------------

def _review_out(db: Session, review: models.AccessReview, items: bool = False) -> dict:
    rows = db.query(models.AccessReviewItem).filter(models.AccessReviewItem.review_id == review.id).order_by(models.AccessReviewItem.id).all()
    out = {"id": review.id, "title": review.title, "status": review.status, "created_at": review.created_at.isoformat(), "completed_at": review.completed_at.isoformat() if review.completed_at else None,
           "total": len(rows), "decided": sum(1 for r in rows if r.decision)}
    if items:
        out["items"] = [{"id": r.id, "user_id": r.user_id, "email": r.email, "name": r.name, "role_at_review": r.role_at_review, "mfa_enabled": r.mfa_enabled, "decision": r.decision,
                         "new_role": r.new_role, "decided_by": r.decided_by, "decided_at": r.decided_at.isoformat() if r.decided_at else None} for r in rows]
    return out


@router.post("/access-reviews")
def start_review(payload: schemas.AccessReviewIn, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    if db.query(models.AccessReview).filter(models.AccessReview.tenant_id == user.tenant_id, models.AccessReview.status == "open").first():
        raise HTTPException(409, "An access review is already open — complete it first")
    review = models.AccessReview(tenant_id=user.tenant_id, title=payload.title.strip()[:120] or "Access review", created_by=user.id)
    db.add(review)
    db.flush()
    locked = {r[0] for r in db.query(models.UserLock.user_id).filter(models.UserLock.tenant_id == user.tenant_id)}
    for u in db.query(models.User).filter(models.User.tenant_id == user.tenant_id).order_by(models.User.id):
        if u.id not in locked:
            db.add(models.AccessReviewItem(review_id=review.id, tenant_id=user.tenant_id, user_id=u.id, email=u.email, name=u.name, role_at_review=u.role, mfa_enabled=bool(u.mfa_enabled)))
    log_audit(db, user.tenant_id, user, "compliance.access_review_started", "access_review", review.id, after={"title": review.title})
    db.commit()
    return _review_out(db, review, items=True)


@router.get("/access-reviews")
def list_reviews(db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    return [_review_out(db, r) for r in db.query(models.AccessReview).filter(models.AccessReview.tenant_id == user.tenant_id).order_by(models.AccessReview.id.desc())]


def _get_review(db: Session, user: models.User, review_id: int) -> models.AccessReview:
    review = db.query(models.AccessReview).filter(models.AccessReview.id == review_id, models.AccessReview.tenant_id == user.tenant_id).first()
    if review is None:
        raise HTTPException(404, "Review not found")
    return review


@router.get("/access-reviews/{review_id}")
def get_review(review_id: int, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    return _review_out(db, _get_review(db, user, review_id), items=True)


@router.post("/access-reviews/{review_id}/items/{item_id}")
def decide(review_id: int, item_id: int, payload: schemas.ReviewDecisionIn, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    review = _get_review(db, user, review_id)
    if review.status != "open":
        raise HTTPException(409, "This review is already completed")
    item = db.query(models.AccessReviewItem).filter(models.AccessReviewItem.id == item_id, models.AccessReviewItem.review_id == review.id).first()
    if item is None:
        raise HTTPException(404, "Item not found")
    if item.user_id == user.id:
        raise HTTPException(403, "You can't review your own access — another organization admin must decide this one")
    if payload.decision not in ("keep", "change_role", "revoke"):
        raise HTTPException(400, "Decision must be keep, change_role or revoke")
    target = db.query(models.User).filter(models.User.id == item.user_id, models.User.tenant_id == user.tenant_id).first()
    if payload.decision == "change_role":
        if payload.new_role not in ROLES:
            raise HTTPException(400, f"new_role must be one of: {', '.join(ROLES)}")
        if target.role == "org_admin" and payload.new_role != "org_admin" and _active_admin_ids(db, user.tenant_id) == [target.id]:
            raise HTTPException(409, "That would remove the workspace's last organization admin")
        before, target.role = target.role, payload.new_role
        log_audit(db, user.tenant_id, user, "user.role_changed_by_review", "User", target.id, before={"role": before}, after={"role": payload.new_role, "review_id": review.id})
    elif payload.decision == "revoke":
        if target.role == "org_admin" and _active_admin_ids(db, user.tenant_id) == [target.id]:
            raise HTTPException(409, "That would lock out the workspace's last organization admin")
        if db.query(models.UserLock).filter(models.UserLock.user_id == target.id).first() is None:
            db.add(models.UserLock(tenant_id=user.tenant_id, user_id=target.id, reason=f"access revoked in review #{review.id}", created_by=user.id))
        log_audit(db, user.tenant_id, user, "user.access_revoked_by_review", "User", target.id, after={"review_id": review.id})
    item.decision, item.new_role, item.decided_by, item.decided_at = payload.decision, payload.new_role if payload.decision == "change_role" else None, user.id, datetime.utcnow()
    db.commit()
    return _review_out(db, review)


@router.post("/access-reviews/{review_id}/complete")
def complete_review(review_id: int, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    review = _get_review(db, user, review_id)
    out = _review_out(db, review)
    if review.status != "open":
        raise HTTPException(409, "This review is already completed")
    if out["decided"] < out["total"]:
        raise HTTPException(409, f"{out['total'] - out['decided']} of {out['total']} access decisions are still pending")
    review.status, review.completed_at = "completed", datetime.utcnow()
    log_audit(db, user.tenant_id, user, "compliance.access_review_completed", "access_review", review.id, after={"total": out["total"]})
    db.commit()
    return _review_out(db, review)


# ---------------------------------------------------------------------------
# Breach register (GDPR Art. 33)
# ---------------------------------------------------------------------------

def _breach_out(b: models.BreachIncident) -> dict:
    deadline = b.discovered_at + timedelta(hours=compliance.BREACH_DEADLINE_HOURS)
    remaining = (deadline - datetime.utcnow()).total_seconds() / 3600
    return {"id": b.id, "title": b.title, "description": b.description, "severity": b.severity, "affected_count": b.affected_count, "status": b.status,
            "discovered_at": b.discovered_at.isoformat(), "authority_deadline": deadline.isoformat(), "hours_remaining": round(remaining, 1),
            "overdue": b.authority_notified_at is None and remaining < 0, "authority_notified_at": b.authority_notified_at.isoformat() if b.authority_notified_at else None,
            "subjects_notified_at": b.subjects_notified_at.isoformat() if b.subjects_notified_at else None}


@router.post("/breaches")
def report_breach(payload: schemas.BreachIn, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    if not payload.title.strip() or payload.severity not in ("low", "medium", "high"):
        raise HTTPException(400, "A title and a severity of low, medium or high are required")
    try:
        discovered = datetime.fromisoformat(payload.discovered_at.replace("Z", "+00:00")).replace(tzinfo=None) if payload.discovered_at else datetime.utcnow()
    except ValueError:
        raise HTTPException(400, "discovered_at must be an ISO 8601 timestamp")
    b = models.BreachIncident(tenant_id=user.tenant_id, title=payload.title.strip()[:200], description=payload.description[:2000], severity=payload.severity,
                              affected_count=payload.affected_count, discovered_at=discovered, created_by=user.id)
    db.add(b)
    db.flush()
    log_audit(db, user.tenant_id, user, "compliance.breach_reported", "breach", b.id, after={"title": b.title, "severity": b.severity})
    db.commit()
    return _breach_out(b)


@router.get("/breaches")
def list_breaches(db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    return [_breach_out(b) for b in db.query(models.BreachIncident).filter(models.BreachIncident.tenant_id == user.tenant_id).order_by(models.BreachIncident.id.desc())]


@router.patch("/breaches/{breach_id}")
def update_breach(breach_id: int, payload: schemas.BreachUpdate, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    b = db.query(models.BreachIncident).filter(models.BreachIncident.id == breach_id, models.BreachIncident.tenant_id == user.tenant_id).first()
    if b is None:
        raise HTTPException(404, "Incident not found")
    now = datetime.utcnow()
    if payload.authority_notified and b.authority_notified_at is None:
        b.authority_notified_at = now
    if payload.subjects_notified and b.subjects_notified_at is None:
        b.subjects_notified_at = now
    if payload.status in ("open", "closed"):
        b.status = payload.status
    log_audit(db, user.tenant_id, user, "compliance.breach_updated", "breach", b.id, after={"status": b.status, "authority_notified": b.authority_notified_at is not None})
    db.commit()
    return _breach_out(b)


# ---------------------------------------------------------------------------
# Instant access revocation (Zero Trust)
# ---------------------------------------------------------------------------

@router.get("/users")
def members(db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    locks = {l.user_id: l.reason for l in db.query(models.UserLock).filter(models.UserLock.tenant_id == user.tenant_id)}
    rows = db.query(models.User).filter(models.User.tenant_id == user.tenant_id).order_by(models.User.id).all()
    return [{"id": u.id, "name": u.name, "email": u.email, "role": u.role, "mfa_enabled": bool(u.mfa_enabled), "suspended": u.id in locks, "suspended_reason": locks.get(u.id)} for u in rows]


@router.post("/users/{user_id}/suspend")
def suspend_user(user_id: int, payload: schemas.SuspendIn, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    target = db.query(models.User).filter(models.User.id == user_id, models.User.tenant_id == user.tenant_id).first()
    if target is None:
        raise HTTPException(404, "User not found")
    if target.id == user.id:
        raise HTTPException(409, "You can't suspend yourself")
    if target.role == "org_admin" and _active_admin_ids(db, user.tenant_id) == [target.id]:
        raise HTTPException(409, "That would lock out the workspace's last organization admin")
    if not payload.reason.strip():
        raise HTTPException(400, "A reason is required")
    if db.query(models.UserLock).filter(models.UserLock.user_id == target.id).first() is None:
        db.add(models.UserLock(tenant_id=user.tenant_id, user_id=target.id, reason=payload.reason.strip()[:300], created_by=user.id))
    log_audit(db, user.tenant_id, user, "user.suspended", "User", target.id, after={"reason": payload.reason.strip()[:300]})
    db.commit()
    return {"suspended": True, "user_id": target.id}


@router.post("/users/{user_id}/restore")
def restore_user(user_id: int, db: Session = Depends(get_db), user: models.User = Depends(require_role("org_admin"))):
    lock = db.query(models.UserLock).filter(models.UserLock.user_id == user_id, models.UserLock.tenant_id == user.tenant_id).first()
    if lock is None:
        raise HTTPException(404, "That user isn't suspended")
    if lock.reason.startswith("erased"):
        raise HTTPException(409, "An erased account can't be restored")
    db.delete(lock)
    log_audit(db, user.tenant_id, user, "user.restored", "User", user_id, after={})
    db.commit()
    return {"restored": True, "user_id": user_id}
