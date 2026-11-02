"""
Feature flag registry and per-tenant evaluation.

Flags are defined once, here, with a documented default. A tenant gets
that default until someone flips it — flipping is what creates the
per-tenant row (see models.FeatureFlag), so there is nothing to seed or
backfill when a new flag is added or a new tenant signs up.

The four flags below gate the four tenant-visible features added in this
pass — real-time updates, the plugin system, multi-agent hand-off, and
white-label branding — so this registry is doing real work from the
moment it exists, not sitting empty until "someday" a flag gets added.
"""

from typing import Optional

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

import models
from auth import get_current_user
from database import get_db

# key -> (label, description, default_enabled)
REGISTRY = {
    "realtime_updates": (
        "Real-time updates",
        "Live push over WebSockets for deal changes, support messages, and agent hand-off progress. "
        "When off, the app is plain request/response and the server refuses new WebSocket connections.",
        True,
    ),
    "plugin_system": (
        "Plugin system",
        "Loads and exposes drop-in plugins discovered in backend/plugins/. When off, every plugin route "
        "is blocked for this workspace regardless of each plugin's individual toggle.",
        True,
    ),
    "multi_agent_handoff": (
        "Multi-agent hand-off",
        "Planner \u2192 executor orchestration in Agent Builder: breaks a goal into steps and runs them "
        "in sequence, each one handed the previous steps' output as context.",
        True,
    ),
    "white_label_branding": (
        "White-label branding",
        "Lets an org admin set a custom workspace name, logo, and color palette that replaces the "
        "default Slate branding throughout the app.",
        False,
    ),
    "enterprise_ldap": (
        "Enterprise LDAP login",
        "Lets people sign in against a configured company LDAP directory instead of a local password. "
        "Off by default \u2014 an org admin configures and tests a real directory connection before turning it on.",
        False,
    ),
    "enterprise_saml": (
        "Enterprise SAML SSO",
        "Lets people sign in through a configured SAML 2.0 identity provider (Okta, Azure AD, OneLogin, "
        "and similar). Off by default \u2014 requires the IdP's SSO URL and signing certificate to be "
        "configured first.",
        False,
    ),
    "enterprise_oauth": (
        "Enterprise OAuth / OpenID Connect login",
        "Lets people sign in through a configured OpenID Connect provider (Google, Microsoft, Okta, "
        "Auth0, Keycloak, and similar) using the authorization-code flow. Off by default \u2014 requires "
        "the provider's endpoints and a client ID and secret to be configured first.",
        False,
    ),
    "workflow_engine": (
        "Workflow engine",
        "n8n/Zapier-style automation graphs in Automation Studio: webhook, schedule, event and manual "
        "triggers; HTTP, AI, branching, parallel and error-handling nodes. Off by default because "
        "workflows can call external URLs and send email on the workspace's behalf.",
        False,
    ),
    "knowledge_rag": (
        "Knowledge search & answers (RAG)",
        "Index uploaded documents (PDF, Word, text) and knowledge-base articles into a vector index, then search "
        "them by meaning and ask questions answered from your own documents with citations. Off by default: "
        "it sends document text to the configured embedding and AI providers.",
        False,
    ),
    "ml_lifecycle": (
        "ML lifecycle (train, deploy, monitor)",
        "Upload tabular datasets, train and compare models, promote versions through staging to production behind a "
        "quality gate, serve predictions, and monitor drift and live accuracy. Off by default: training is compute-heavy.",
        False,
    ),
    "passkeys": (
        "Passkeys (WebAuthn)",
        "Lets people register a platform authenticator or hardware security key and sign in without a "
        "password. On by default \u2014 unlike LDAP/SAML, there's no external directory or identity "
        "provider to configure first; it's entirely self-contained.",
        True,
    ),
}


def default_for(key: str) -> bool:
    entry = REGISTRY.get(key)
    return entry[2] if entry else False


def get_all_flags(db: Session, tenant_id: int) -> dict:
    """Registry defaults overlaid with any per-tenant rows on top."""
    overrides = {
        f.key: f.enabled
        for f in db.query(models.FeatureFlag).filter(models.FeatureFlag.tenant_id == tenant_id).all()
    }
    return {key: overrides.get(key, default_enabled) for key, (_, _, default_enabled) in REGISTRY.items()}


def is_enabled(db: Session, tenant_id: int, key: str) -> bool:
    row = (
        db.query(models.FeatureFlag)
        .filter(models.FeatureFlag.tenant_id == tenant_id, models.FeatureFlag.key == key)
        .first()
    )
    return row.enabled if row is not None else default_for(key)


def set_flag(db: Session, tenant_id: int, key: str, enabled: bool, user: Optional[models.User]) -> models.FeatureFlag:
    """Flushes but does not commit — same convention as the rest of this
    codebase's write helpers (see security.log_audit): the caller owns
    the transaction boundary and commits once, after any audit entry."""
    if key not in REGISTRY:
        raise HTTPException(404, f"Unknown feature flag '{key}'")
    row = (
        db.query(models.FeatureFlag)
        .filter(models.FeatureFlag.tenant_id == tenant_id, models.FeatureFlag.key == key)
        .first()
    )
    if row is None:
        row = models.FeatureFlag(tenant_id=tenant_id, key=key, enabled=enabled)
        db.add(row)
    else:
        row.enabled = enabled
    row.updated_by_user_id = user.id if user else None
    db.flush()
    return row


def require_flag(key: str):
    """FastAPI dependency factory, same shape as auth.require_role — use
    as Depends(require_flag('plugin_system')) on any endpoint that should
    be gated. Enforced server-side; a hidden nav item is not what
    actually turns this off."""

    def checker(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)) -> models.User:
        if not is_enabled(db, user.tenant_id, key):
            label = REGISTRY.get(key, (key,))[0]
            raise HTTPException(403, f"'{label}' is turned off for this workspace")
        return user

    return checker
