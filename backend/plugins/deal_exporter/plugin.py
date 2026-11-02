"""
Reference plugin for the auto-discovery loader (see plugins_loader.py).
Demonstrates the whole contract: a bare `router` with relative paths,
real tenant-scoped data access via the normal `get_current_user` +
`get_db` dependencies, mounted with zero changes to main.py or any other
core file — dropping this folder in (or removing it) is the entire
install/uninstall.
"""

import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

import models
from auth import get_current_user
from database import get_db

router = APIRouter()


@router.get("/export.csv")
def export_deals_csv(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    deals = (
        db.query(models.Deal)
        .filter(models.Deal.tenant_id == user.tenant_id)
        .order_by(models.Deal.created_at.desc())
        .all()
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Deal", "Company", "Stage", "Value", "Created"])
    for d in deals:
        writer.writerow([d.name, d.party.display_name if d.party else "", d.stage, d.value, d.created_at.isoformat()])
    buffer.seek(0)

    return StreamingResponse(
        buffer, media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="deals.csv"'},
    )
