from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import models
import schemas
from auth import get_current_user, require_role
from database import get_db
from feature_flags import is_enabled
from plugins_loader import get_loaded_plugins
from security import log_audit

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


@router.get("", response_model=list[schemas.PluginOut])
def list_plugins(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    overrides = {
        r.plugin_key: r.enabled
        for r in db.query(models.InstalledPlugin).filter(models.InstalledPlugin.tenant_id == user.tenant_id).all()
    }
    system_on = is_enabled(db, user.tenant_id, "plugin_system")
    return [
        {
            "key": p.key, "name": p.name, "version": p.version, "description": p.description,
            "route_count": p.route_count, "load_error": p.error,
            "enabled": overrides.get(p.key, True) and system_on,
        }
        for p in get_loaded_plugins()
    ]


@router.patch("/{key}", response_model=schemas.PluginOut)
def toggle_plugin(
    key: str,
    payload: schemas.PluginUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("org_admin")),
):
    plugin = next((p for p in get_loaded_plugins() if p.key == key), None)
    if not plugin:
        raise HTTPException(404, "Plugin not found")

    row = (
        db.query(models.InstalledPlugin)
        .filter(models.InstalledPlugin.tenant_id == user.tenant_id, models.InstalledPlugin.plugin_key == key)
        .first()
    )
    if row is None:
        row = models.InstalledPlugin(tenant_id=user.tenant_id, plugin_key=key, enabled=payload.enabled)
        db.add(row)
    else:
        row.enabled = payload.enabled
    db.flush()
    log_audit(db, user.tenant_id, user, "plugin.toggled", "plugin", None, after={"key": key, "enabled": payload.enabled})
    db.commit()

    system_on = is_enabled(db, user.tenant_id, "plugin_system")
    return {
        "key": plugin.key, "name": plugin.name, "version": plugin.version, "description": plugin.description,
        "route_count": plugin.route_count, "load_error": plugin.error, "enabled": row.enabled and system_on,
    }
