from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import ai_providers
import models
import schemas
from auth import get_current_user, require_role
from database import get_db

router = APIRouter(prefix="/api/ai-studio", tags=["ai-studio"])


def get_effective_provider_model(db: Session, tenant: models.Tenant) -> tuple:
    """The one place every AI-calling router (copilot, agents) asks
    'which provider/model does this tenant actually use' — so Copilot,
    single-agent invoke, and multi-agent hand-off can never drift out of
    sync with each other or with what this settings screen shows. Takes
    the already-loaded Tenant object (every caller has one on hand
    already) rather than a bare tenant_id, so this never needs its own
    redundant Tenant query."""
    if tenant is None:
        return "anthropic", ai_providers.default_model("anthropic")

    config = db.query(models.AIProviderConfig).filter(models.AIProviderConfig.tenant_id == tenant.id).first()
    if config is not None:
        if config.provider.startswith("custom:"):
            # Ownership is re-verified on every read, against the *calling* tenant: the string
            # handed to ai_providers carries that verified tenant id, so a row that somehow
            # names another workspace's endpoint resolves to nothing instead of to their server.
            endpoint = _tenant_endpoint(db, tenant.id, config.provider)
            if endpoint is None:
                return "custom:invalid", ""
            return f"custom:{tenant.id}:{endpoint.id}", (config.model or endpoint.model)
        return config.provider, (config.model or ai_providers.default_model(config.provider))

    # No row yet: honor a pre-existing Tenant.preferred_model (from before
    # multi-provider support existed) once, rather than silently resetting
    # an existing tenant's preference the first time they load this screen.
    if tenant.preferred_model:
        return "anthropic", tenant.preferred_model
    return "anthropic", ai_providers.default_model("anthropic")


def _tenant_endpoint(db: Session, tenant_id: int, provider: str):
    """The tenant's own enabled chat endpoint named by a stored 'custom:<id>', or None."""
    try:
        endpoint_id = int(provider.split(":")[1])
    except (IndexError, ValueError):
        return None
    return db.query(models.CustomModelEndpoint).filter(
        models.CustomModelEndpoint.id == endpoint_id, models.CustomModelEndpoint.tenant_id == tenant_id,
        models.CustomModelEndpoint.kind == "chat", models.CustomModelEndpoint.enabled == True,  # noqa: E712
    ).first()


def _settings_out(db: Session, tenant: models.Tenant) -> dict:
    provider, model = get_effective_provider_model(db, tenant)
    if provider.startswith("custom:") and provider != "custom:invalid":
        provider = f"custom:{provider.split(':')[2]}"  # the stored, tenant-relative form the UI selects by
    available = [
        {"key": key, "name": meta["name"], "default_model": meta["default_model"], "is_configured": ai_providers.is_configured(key)}
        for key, meta in ai_providers.PROVIDERS.items()
    ]
    for endpoint in db.query(models.CustomModelEndpoint).filter(
        models.CustomModelEndpoint.tenant_id == tenant.id, models.CustomModelEndpoint.kind == "chat",
        models.CustomModelEndpoint.enabled == True,  # noqa: E712
    ).order_by(models.CustomModelEndpoint.id):
        available.append({"key": f"custom:{endpoint.id}", "name": f"{endpoint.name} (custom endpoint)", "default_model": endpoint.model, "is_configured": True})
    return {"provider": provider, "model": model, "available_providers": available}


@router.get("/settings", response_model=schemas.AIStudioSettings)
def get_settings(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    return _settings_out(db, tenant)


@router.patch("/settings", response_model=schemas.AIStudioSettings)
def update_settings(
    payload: schemas.AIStudioUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_role("org_admin")),
):
    """Not cosmetic — routers/copilot.py and routers/agents.py both call
    get_effective_provider_model() on every request. Changing this here
    changes which provider and model actually answer the next Copilot
    question or hand-off run."""
    if payload.provider.startswith("custom:"):
        if _tenant_endpoint(db, user.tenant_id, payload.provider) is None:
            raise HTTPException(400, "That custom endpoint doesn't exist in this workspace (or is disabled)")
    elif payload.provider not in ai_providers.PROVIDERS:
        raise HTTPException(400, f"Unknown provider '{payload.provider}'")

    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    config = db.query(models.AIProviderConfig).filter(models.AIProviderConfig.tenant_id == user.tenant_id).first()
    if config is None:
        config = models.AIProviderConfig(tenant_id=user.tenant_id)
        db.add(config)
    config.provider = payload.provider
    config.model = payload.model or None
    db.commit()

    return _settings_out(db, tenant)
