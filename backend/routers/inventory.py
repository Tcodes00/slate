from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db

router = APIRouter(prefix="/api/inventory", tags=["inventory"])


def _item_out(item: models.InventoryItem) -> dict:
    return {
        "id": item.id,
        "sku": item.sku,
        "name": item.name,
        "unit_of_measure": item.unit_of_measure,
        "quantity_on_hand": item.quantity_on_hand,
        "reorder_point": item.reorder_point,
        "unit_cost": item.unit_cost,
        "low_stock": item.quantity_on_hand <= item.reorder_point,
    }


@router.get("/items", response_model=list[schemas.InventoryItemOut])
def list_items(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    items = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.tenant_id == user.tenant_id)
        .order_by(models.InventoryItem.name)
        .all()
    )
    return [_item_out(i) for i in items]


@router.get("/low-stock", response_model=list[schemas.InventoryItemOut])
def low_stock(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    items = db.query(models.InventoryItem).filter(models.InventoryItem.tenant_id == user.tenant_id).all()
    return [_item_out(i) for i in items if i.quantity_on_hand <= i.reorder_point]


@router.post("/items", response_model=schemas.InventoryItemOut)
def create_item(
    payload: schemas.InventoryItemCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    item = models.InventoryItem(tenant_id=user.tenant_id, **payload.dict())
    db.add(item)
    db.flush()
    if item.quantity_on_hand:
        db.add(models.InventoryMovement(
            tenant_id=user.tenant_id, item_id=item.id, movement_type="in",
            quantity=item.quantity_on_hand, note="Initial stock on creation",
        ))
    db.commit()
    db.refresh(item)
    return _item_out(item)


@router.get("/items/{item_id}/movements", response_model=list[schemas.MovementOut])
def list_movements(
    item_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)
):
    return (
        db.query(models.InventoryMovement)
        .filter(models.InventoryMovement.item_id == item_id, models.InventoryMovement.tenant_id == user.tenant_id)
        .order_by(models.InventoryMovement.occurred_at.desc())
        .all()
    )


@router.post("/items/{item_id}/movements", response_model=schemas.InventoryItemOut)
def record_movement(
    item_id: int,
    payload: schemas.MovementCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("standard_user")),
):
    item = (
        db.query(models.InventoryItem)
        .filter(models.InventoryItem.id == item_id, models.InventoryItem.tenant_id == user.tenant_id)
        .first()
    )
    if not item:
        raise HTTPException(404, "Item not found")
    if payload.movement_type not in ("in", "out", "adjustment"):
        raise HTTPException(400, "movement_type must be 'in', 'out', or 'adjustment'")

    if payload.movement_type == "in":
        item.quantity_on_hand += payload.quantity
    elif payload.movement_type == "out":
        if payload.quantity > item.quantity_on_hand:
            raise HTTPException(400, "Not enough stock on hand for this movement")
        item.quantity_on_hand -= payload.quantity
    else:  # adjustment sets the absolute quantity, e.g. after a physical count
        item.quantity_on_hand = payload.quantity

    db.add(models.InventoryMovement(
        tenant_id=user.tenant_id, item_id=item.id, movement_type=payload.movement_type,
        quantity=payload.quantity, note=payload.note,
    ))
    db.commit()
    db.refresh(item)
    return _item_out(item)
