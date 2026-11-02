from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db
from events import publish_event
from pdf_gen import build_invoice_pdf

router = APIRouter(prefix="/api/finance", tags=["finance"])


def _invoice_out(inv: models.Invoice) -> dict:
    is_overdue = bool(
        inv.status == "Sent" and inv.due_date is not None and inv.due_date < datetime.utcnow()
    )
    return {
        "id": inv.id,
        "invoice_number": inv.invoice_number,
        "company": inv.party.display_name if inv.party else "",
        "description": inv.description,
        "amount": inv.amount,
        "status": inv.status,
        "due_date": inv.due_date,
        "issued_at": inv.issued_at,
        "paid_at": inv.paid_at,
        "is_overdue": is_overdue,
    }


def _next_invoice_number(db: Session, tenant_id: int) -> str:
    count = db.query(models.Invoice).filter(models.Invoice.tenant_id == tenant_id).count()
    return f"INV-{count + 1:04d}"


@router.get("/invoices", response_model=list[schemas.InvoiceOut])
def list_invoices(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    q = db.query(models.Invoice).filter(models.Invoice.tenant_id == user.tenant_id)
    if status:
        q = q.filter(models.Invoice.status == status)
    invoices = q.order_by(models.Invoice.issued_at.desc()).all()
    return [_invoice_out(i) for i in invoices]


@router.post("/invoices", response_model=schemas.InvoiceOut)
def create_invoice(
    payload: schemas.InvoiceCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    party_id = payload.party_id
    if not party_id:
        if not payload.company:
            raise HTTPException(400, "Provide either party_id or company")
        party = models.Party(
            tenant_id=user.tenant_id,
            party_type="organization",
            display_name=payload.company,
            owner_user_id=user.id,
        )
        db.add(party)
        db.flush()
        party_id = party.id

    invoice = models.Invoice(
        tenant_id=user.tenant_id,
        party_id=party_id,
        deal_id=payload.deal_id,
        invoice_number=_next_invoice_number(db, user.tenant_id),
        description=payload.description,
        amount=payload.amount,
        due_date=payload.due_date,
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return _invoice_out(invoice)


@router.post("/generate-from-deal/{deal_id}", response_model=schemas.InvoiceOut)
def generate_from_deal(
    deal_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    deal = (
        db.query(models.Deal)
        .filter(models.Deal.id == deal_id, models.Deal.tenant_id == user.tenant_id)
        .first()
    )
    if not deal:
        raise HTTPException(404, "Deal not found")

    existing = db.query(models.Invoice).filter(models.Invoice.deal_id == deal_id).first()
    if existing:
        return _invoice_out(existing)

    invoice = models.Invoice(
        tenant_id=user.tenant_id,
        party_id=deal.party_id,
        deal_id=deal.id,
        invoice_number=_next_invoice_number(db, user.tenant_id),
        description=deal.name,
        amount=deal.value,
        due_date=datetime.utcnow() + timedelta(days=30),
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return _invoice_out(invoice)


@router.patch("/invoices/{invoice_id}", response_model=schemas.InvoiceOut)
def update_invoice(
    invoice_id: int,
    payload: schemas.InvoiceUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    invoice = (
        db.query(models.Invoice)
        .filter(models.Invoice.id == invoice_id, models.Invoice.tenant_id == user.tenant_id)
        .first()
    )
    if not invoice:
        raise HTTPException(404, "Invoice not found")

    if payload.status is not None:
        invoice.status = payload.status
        if payload.status == "Paid":
            invoice.paid_at = datetime.utcnow()
        publish_event(user.tenant_id, "invoice.updated", {
            "id": invoice.id, "invoice_number": invoice.invoice_number, "status": invoice.status,
            "company": invoice.party.display_name if invoice.party else None, "amount": invoice.amount,
        })
    if payload.amount is not None:
        invoice.amount = payload.amount
    if payload.due_date is not None:
        invoice.due_date = payload.due_date

    db.commit()
    db.refresh(invoice)
    return _invoice_out(invoice)


@router.get("/summary")
def finance_summary(
    db: Session = Depends(get_db), user: models.User = Depends(get_current_user)
):
    invoices = db.query(models.Invoice).filter(models.Invoice.tenant_id == user.tenant_id).all()
    outstanding = sum(i.amount for i in invoices if i.status in ("Draft", "Sent"))
    paid = sum(i.amount for i in invoices if i.status == "Paid")
    overdue = [
        i for i in invoices
        if i.status == "Sent" and i.due_date and i.due_date < datetime.utcnow()
    ]
    return {
        "outstanding": outstanding,
        "paid": paid,
        "overdue_count": len(overdue),
        "overdue_amount": sum(i.amount for i in overdue),
        "invoice_count": len(invoices),
    }


@router.get("/invoices/{invoice_id}/pdf")
def download_invoice_pdf(
    invoice_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)
):
    invoice = (
        db.query(models.Invoice)
        .filter(models.Invoice.id == invoice_id, models.Invoice.tenant_id == user.tenant_id)
        .first()
    )
    if not invoice:
        raise HTTPException(404, "Invoice not found")

    pdf_bytes = build_invoice_pdf(_invoice_out(invoice))
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{invoice.invoice_number}.pdf"'},
    )
