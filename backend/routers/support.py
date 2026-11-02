from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db
from events import publish_event

router = APIRouter(prefix="/api/support", tags=["support"])


def _ticket_out(db: Session, t: models.Ticket) -> dict:
    count = db.query(models.TicketMessage).filter(models.TicketMessage.ticket_id == t.id).count()
    return {
        "id": t.id,
        "subject": t.subject,
        "company": t.party.display_name if t.party else None,
        "status": t.status,
        "priority": t.priority,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
        "message_count": count,
    }


@router.get("/tickets", response_model=list[schemas.TicketOut])
def list_tickets(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    tickets = (
        db.query(models.Ticket).filter(models.Ticket.tenant_id == user.tenant_id)
        .order_by(models.Ticket.updated_at.desc()).all()
    )
    return [_ticket_out(db, t) for t in tickets]


@router.post("/tickets", response_model=schemas.TicketOut)
def create_ticket(
    payload: schemas.TicketCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    party_id = payload.party_id
    if not party_id and payload.company:
        party = models.Party(tenant_id=user.tenant_id, party_type="organization",
                              display_name=payload.company, owner_user_id=user.id)
        db.add(party)
        db.flush()
        party_id = party.id

    ticket = models.Ticket(
        tenant_id=user.tenant_id, party_id=party_id, subject=payload.subject, priority=payload.priority,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)
    publish_event(user.tenant_id, "ticket.created", {
        "id": ticket.id, "subject": ticket.subject, "priority": ticket.priority,
        "company": ticket.party.display_name if ticket.party else None,
    })
    return _ticket_out(db, ticket)


@router.patch("/tickets/{ticket_id}", response_model=schemas.TicketOut)
def update_ticket(
    ticket_id: int,
    payload: schemas.TicketUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    ticket = db.query(models.Ticket).filter(
        models.Ticket.id == ticket_id, models.Ticket.tenant_id == user.tenant_id
    ).first()
    if not ticket:
        raise HTTPException(404, "Ticket not found")
    if payload.status is not None:
        ticket.status = payload.status
    if payload.priority is not None:
        ticket.priority = payload.priority
    db.commit()
    db.refresh(ticket)
    return _ticket_out(db, ticket)


@router.get("/tickets/{ticket_id}/messages", response_model=list[schemas.TicketMessageOut])
def list_messages(ticket_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    return (
        db.query(models.TicketMessage)
        .filter(models.TicketMessage.ticket_id == ticket_id, models.TicketMessage.tenant_id == user.tenant_id)
        .order_by(models.TicketMessage.created_at)
        .all()
    )


@router.post("/tickets/{ticket_id}/messages", response_model=schemas.TicketMessageOut)
def add_message(
    ticket_id: int,
    payload: schemas.TicketMessageCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    ticket = db.query(models.Ticket).filter(
        models.Ticket.id == ticket_id, models.Ticket.tenant_id == user.tenant_id
    ).first()
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    message = models.TicketMessage(
        tenant_id=user.tenant_id, ticket_id=ticket_id, sender=payload.sender, content=payload.content,
    )
    db.add(message)
    ticket.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(message)
    publish_event(user.tenant_id, "ticket.message", {
        "ticket_id": ticket_id, "subject": ticket.subject, "sender": message.sender,
        "content": message.content, "created_at": message.created_at,
    })
    return message
