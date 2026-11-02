from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db

router = APIRouter(prefix="/api/sales", tags=["sales"])


def _quote_out(q: models.Quote) -> dict:
    return {
        "id": q.id, "quote_number": q.quote_number,
        "company": q.party.display_name if q.party else "",
        "description": q.description, "amount": q.amount,
        "status": q.status, "created_at": q.created_at,
    }


def _order_out(o: models.Order) -> dict:
    return {
        "id": o.id, "order_number": o.order_number,
        "company": o.party.display_name if o.party else "",
        "description": o.description, "amount": o.amount,
        "status": o.status, "created_at": o.created_at,
    }


def _next_number(db: Session, tenant_id: int, model, prefix: str) -> str:
    count = db.query(model).filter(model.tenant_id == tenant_id).count()
    return f"{prefix}-{count + 1:04d}"


@router.get("/quotes", response_model=list[schemas.QuoteOut])
def list_quotes(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    quotes = (
        db.query(models.Quote).filter(models.Quote.tenant_id == user.tenant_id)
        .order_by(models.Quote.created_at.desc()).all()
    )
    return [_quote_out(q) for q in quotes]


@router.post("/quotes", response_model=schemas.QuoteOut)
def create_quote(
    payload: schemas.QuoteCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    party_id = payload.party_id
    if payload.deal_id:
        deal = db.query(models.Deal).filter(
            models.Deal.id == payload.deal_id, models.Deal.tenant_id == user.tenant_id
        ).first()
        if not deal:
            raise HTTPException(404, "Deal not found")
        party_id = deal.party_id
    elif not party_id:
        if not payload.company:
            raise HTTPException(400, "Provide deal_id, party_id, or company")
        party = models.Party(tenant_id=user.tenant_id, party_type="organization",
                              display_name=payload.company, owner_user_id=user.id)
        db.add(party)
        db.flush()
        party_id = party.id

    quote = models.Quote(
        tenant_id=user.tenant_id, deal_id=payload.deal_id, party_id=party_id,
        quote_number=_next_number(db, user.tenant_id, models.Quote, "Q"),
        description=payload.description, amount=payload.amount,
    )
    db.add(quote)
    db.commit()
    db.refresh(quote)
    return _quote_out(quote)


@router.patch("/quotes/{quote_id}", response_model=schemas.QuoteOut)
def update_quote(
    quote_id: int,
    payload: schemas.QuoteUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    quote = db.query(models.Quote).filter(
        models.Quote.id == quote_id, models.Quote.tenant_id == user.tenant_id
    ).first()
    if not quote:
        raise HTTPException(404, "Quote not found")

    quote.status = payload.status

    # Accepting a quote creates its order automatically — the same pattern
    # as a Won deal creating an invoice. One click, real state change.
    if payload.status == "Accepted":
        existing_order = db.query(models.Order).filter(models.Order.quote_id == quote.id).first()
        if not existing_order:
            order = models.Order(
                tenant_id=user.tenant_id, quote_id=quote.id, party_id=quote.party_id,
                order_number=_next_number(db, user.tenant_id, models.Order, "ORD"),
                description=quote.description, amount=quote.amount,
            )
            db.add(order)

    db.commit()
    db.refresh(quote)
    return _quote_out(quote)


@router.get("/orders", response_model=list[schemas.OrderOut])
def list_orders(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    orders = (
        db.query(models.Order).filter(models.Order.tenant_id == user.tenant_id)
        .order_by(models.Order.created_at.desc()).all()
    )
    return [_order_out(o) for o in orders]


@router.patch("/orders/{order_id}", response_model=schemas.OrderOut)
def update_order(
    order_id: int,
    payload: schemas.OrderUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    order = db.query(models.Order).filter(
        models.Order.id == order_id, models.Order.tenant_id == user.tenant_id
    ).first()
    if not order:
        raise HTTPException(404, "Order not found")
    order.status = payload.status
    db.commit()
    db.refresh(order)
    return _order_out(order)
