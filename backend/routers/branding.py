"""
Real per-tenant white-labeling: a workspace name, logo, and two colors
that flow all the way to the rendered UI (see frontend/components/
BrandingProvider.js, which sets them as CSS custom properties every
existing component already reads), not a source-code find-and-replace of
the word "Slate". Reading branding for a tenant with no saved row returns
the platform defaults instead of requiring a migration to backfill every
tenant that existed before this feature did.
"""

import re

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db
from feature_flags import is_enabled
from security import log_audit
from storage import delete_file, get_file, save_file

router = APIRouter(prefix="/api/branding", tags=["branding"])

DEFAULT_PRIMARY = "#0E7C6B"
DEFAULT_SECONDARY = "#B8792E"
HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _out(tenant: models.Tenant, row: models.TenantBranding) -> dict:
    return {
        "display_name": (row.display_name if row and row.display_name else None) or tenant.name,
        "has_logo": bool(row and row.logo_storage_key),
        "primary_color": (row.primary_color if row else None) or DEFAULT_PRIMARY,
        "secondary_color": (row.secondary_color if row else None) or DEFAULT_SECONDARY,
        "is_customized": row is not None,
    }


def _get_row(db: Session, tenant_id: int) -> models.TenantBranding:
    return db.query(models.TenantBranding).filter(models.TenantBranding.tenant_id == tenant_id).first()


@router.get("", response_model=schemas.BrandingOut)
def get_branding(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    return _out(tenant, _get_row(db, user.tenant_id))


@router.put("", response_model=schemas.BrandingOut)
def update_branding(
    payload: schemas.BrandingUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("org_admin")),
):
    if not is_enabled(db, user.tenant_id, "white_label_branding"):
        raise HTTPException(403, "White-label branding is turned off for this workspace")
    for color in (payload.primary_color, payload.secondary_color):
        if color is not None and not HEX_COLOR_RE.match(color):
            raise HTTPException(400, f"'{color}' isn't a valid hex color (expected e.g. #0E7C6B)")

    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    row = _get_row(db, user.tenant_id)
    if row is None:
        row = models.TenantBranding(tenant_id=user.tenant_id)
        db.add(row)

    if payload.display_name is not None:
        row.display_name = payload.display_name.strip() or None
    if payload.primary_color is not None:
        row.primary_color = payload.primary_color
    if payload.secondary_color is not None:
        row.secondary_color = payload.secondary_color

    db.flush()
    log_audit(db, user.tenant_id, user, "branding.updated", "tenant_branding", row.id)
    db.commit()
    db.refresh(row)
    return _out(tenant, row)


@router.post("/logo", response_model=schemas.BrandingOut)
async def upload_logo(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("org_admin")),
):
    if not is_enabled(db, user.tenant_id, "white_label_branding"):
        raise HTTPException(403, "White-label branding is turned off for this workspace")
    if file.content_type not in ("image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml", "image/x-icon"):
        raise HTTPException(400, "Logo must be an image (PNG, JPEG, WEBP, GIF, or SVG)")

    content = await file.read()
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(400, "Logo must be under 2MB")

    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    row = _get_row(db, user.tenant_id)
    if row is None:
        row = models.TenantBranding(tenant_id=user.tenant_id)
        db.add(row)
        db.flush()

    if row.logo_storage_key:
        delete_file(row.logo_storage_key)

    row.logo_storage_key = save_file(user.tenant_id, file.filename, content)
    row.logo_mime_type = file.content_type
    db.flush()
    log_audit(db, user.tenant_id, user, "branding.logo_uploaded", "tenant_branding", row.id)
    db.commit()
    db.refresh(row)
    return _out(tenant, row)


@router.get("/logo")
def get_logo(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    """Authenticated like every other read in this app, deliberately —
    the frontend fetches this as a blob and hands the browser an object
    URL (see fetchLogoBlobUrl in lib/api.js), the same pattern already
    used for document downloads and invoice PDFs, rather than carving out
    a public exception for logos specifically."""
    row = _get_row(db, user.tenant_id)
    if not row or not row.logo_storage_key:
        raise HTTPException(404, "No logo uploaded")
    content = get_file(row.logo_storage_key)
    return Response(content=content, media_type=row.logo_mime_type or "image/png")
