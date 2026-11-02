"""
Drop-in plugin auto-discovery — proof of concept.

A plugin is a directory under backend/plugins/ containing:
  __init__.py    (empty — makes it an importable package)
  manifest.json  {"key": "...", "name": "...", "version": "...", "description": "..."}
  plugin.py      defines `router = APIRouter()` at module level, with
                 routes on relative paths (no prefix of its own)

Dropping a new folder in and restarting the server is enough to pick it
up — nothing to register by hand in main.py. Each plugin's router is
mounted under /api/plugins/{key}/, wrapped in a dependency that 403s if
the plugin_system flag or the plugin's own per-tenant toggle is off for
the caller's tenant, so that's enforced once, here, rather than something
every plugin author has to remember to add themselves. A plugin whose
manifest or module fails to load is recorded with its error instead of
crashing server startup — one broken drop-in shouldn't take the platform
down.
"""

import importlib
import json
import os
from dataclasses import dataclass
from typing import List, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

import models
from auth import get_current_user
from database import get_db
from feature_flags import is_enabled

PLUGINS_DIR = os.path.join(os.path.dirname(__file__), "plugins")


@dataclass
class LoadedPlugin:
    key: str
    name: str
    version: str
    description: str
    route_count: int
    error: Optional[str] = None


_loaded: List[LoadedPlugin] = []


def _plugin_gate(plugin_key: str):
    """Layered in front of every route a plugin defines, in addition to
    whatever auth the plugin itself declares on top."""

    def checker(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
        if not is_enabled(db, user.tenant_id, "plugin_system"):
            raise HTTPException(403, "Plugins are turned off for this workspace")
        row = (
            db.query(models.InstalledPlugin)
            .filter(models.InstalledPlugin.tenant_id == user.tenant_id, models.InstalledPlugin.plugin_key == plugin_key)
            .first()
        )
        enabled = row.enabled if row is not None else True
        if not enabled:
            raise HTTPException(403, f"The '{plugin_key}' plugin is disabled for this workspace")
        return user

    return checker


def discover_and_mount(app: FastAPI) -> List[LoadedPlugin]:
    _loaded.clear()
    if not os.path.isdir(PLUGINS_DIR):
        return _loaded

    for entry in sorted(os.listdir(PLUGINS_DIR)):
        plugin_path = os.path.join(PLUGINS_DIR, entry)
        manifest_path = os.path.join(plugin_path, "manifest.json")
        if not os.path.isdir(plugin_path) or not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path) as f:
                manifest = json.load(f)
            key = manifest["key"]
            module = importlib.import_module(f"plugins.{entry}.plugin")
            plugin_router: APIRouter = module.router

            gated = APIRouter(prefix=f"/api/plugins/{key}", tags=[f"plugin:{key}"])
            gated.include_router(plugin_router, dependencies=[Depends(_plugin_gate(key))])
            app.include_router(gated)

            _loaded.append(LoadedPlugin(
                key=key, name=manifest.get("name", key), version=manifest.get("version", "0.0.0"),
                description=manifest.get("description", ""), route_count=len(plugin_router.routes),
            ))
        except Exception as e:
            _loaded.append(LoadedPlugin(key=entry, name=entry, version="?", description="", route_count=0, error=str(e)))

    return _loaded


def get_loaded_plugins() -> List[LoadedPlugin]:
    return _loaded
