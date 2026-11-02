from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db
from security import decrypt_field, encrypt_field, log_audit

router = APIRouter(prefix="/api/erp", tags=["erp"])


def _po_out(db: Session, po: models.PurchaseOrder) -> dict:
    requester = db.query(models.User).filter(models.User.id == po.requested_by_user_id).first()
    approver = db.query(models.User).filter(models.User.id == po.approved_by_user_id).first() if po.approved_by_user_id else None
    return {
        "id": po.id, "vendor": po.vendor.party.display_name if po.vendor and po.vendor.party else "",
        "description": po.description, "total": po.total, "status": po.status,
        "requested_by": requester.name if requester else "Unknown",
        "approved_by": approver.name if approver else None,
        "created_at": po.created_at,
    }


@router.get("/vendors", response_model=list[schemas.VendorOut])
def list_vendors(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    vendors = db.query(models.Vendor).filter(models.Vendor.tenant_id == user.tenant_id).all()
    out = []
    for v in vendors:
        bank = None
        if v.bank_details_encrypted and user.role == "org_admin":
            bank = decrypt_field(v.bank_details_encrypted)  # only Org Admin ever sees plaintext
        out.append({
            "id": v.id, "company": v.party.display_name if v.party else "",
            "payment_terms": v.payment_terms, "rating": v.rating,
            "has_bank_details": bool(v.bank_details_encrypted), "bank_details": bank,
        })
    return out


@router.post("/vendors", response_model=schemas.VendorOut)
def create_vendor(payload: schemas.VendorCreate, db: Session = Depends(get_db), user: models.User = Depends(require_role("standard_user"))):
    party = models.Party(tenant_id=user.tenant_id, party_type="organization", display_name=payload.company, owner_user_id=user.id)
    db.add(party)
    db.flush()
    vendor = models.Vendor(
        tenant_id=user.tenant_id, party_id=party.id, payment_terms=payload.payment_terms,
        bank_details_encrypted=encrypt_field(payload.bank_details) if payload.bank_details else None,
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return {
        "id": vendor.id, "company": party.display_name, "payment_terms": vendor.payment_terms,
        "rating": vendor.rating, "has_bank_details": bool(payload.bank_details),
        "bank_details": payload.bank_details if user.role == "org_admin" else None,
    }


@router.get("/purchase-orders", response_model=list[schemas.POOut])
def list_pos(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    pos = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.tenant_id == user.tenant_id).order_by(models.PurchaseOrder.created_at.desc()).all()
    return [_po_out(db, p) for p in pos]


@router.post("/purchase-orders", response_model=schemas.POOut)
def create_po(payload: schemas.POCreate, db: Session = Depends(get_db), user: models.User = Depends(require_role("standard_user"))):
    vendor = db.query(models.Vendor).filter(models.Vendor.id == payload.vendor_id, models.Vendor.tenant_id == user.tenant_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")
    po = models.PurchaseOrder(
        tenant_id=user.tenant_id, vendor_id=payload.vendor_id, requested_by_user_id=user.id,
        description=payload.description, total=payload.total,
    )
    db.add(po)
    db.commit()
    db.refresh(po)
    return _po_out(db, po)


@router.post("/purchase-orders/{po_id}/approve", response_model=schemas.POOut)
def approve_po(po_id: int, db: Session = Depends(get_db), user: models.User = Depends(require_role("manager"))):
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == po_id, models.PurchaseOrder.tenant_id == user.tenant_id).first()
    if not po:
        raise HTTPException(404, "Purchase order not found")
    if po.requested_by_user_id == user.id:
        raise HTTPException(400, "Segregation of duties: you can't approve a purchase order you requested yourself")
    if po.status != "Pending":
        raise HTTPException(400, f"Already {po.status.lower()}")
    before_status = po.status
    po.status = "Approved"
    po.approved_by_user_id = user.id
    log_audit(
        db, user.tenant_id, user, "purchase_order.approved", "PurchaseOrder", po.id,
        before={"status": before_status}, after={"status": "Approved", "total": po.total},
    )
    db.commit()
    db.refresh(po)
    return _po_out(db, po)


@router.post("/purchase-orders/{po_id}/reject", response_model=schemas.POOut)
def reject_po(po_id: int, db: Session = Depends(get_db), user: models.User = Depends(require_role("manager"))):
    po = db.query(models.PurchaseOrder).filter(models.PurchaseOrder.id == po_id, models.PurchaseOrder.tenant_id == user.tenant_id).first()
    if not po:
        raise HTTPException(404, "Purchase order not found")
    po.status = "Rejected"
    po.approved_by_user_id = user.id
    db.commit()
    db.refresh(po)
    return _po_out(db, po)
