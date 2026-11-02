"""
AI Copilot — proxies to whichever LLM provider the tenant has configured
(backend/ai_providers.py), server-side.

Two deliberate design choices, both different from (and better than) a
browser-only demo:
  1. Every provider's API key lives here, in environment variables, never in client JS.
  2. The pipeline context is pulled fresh from the database for the caller's
     own tenant — the client sends only the conversation, not "here's the
     data," so a stale or tampered frontend can't feed the model wrong
     numbers and have it repeat them back as fact.
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import ai_providers
import models
from auth import get_current_user
from database import get_db
from routers.ai_studio import get_effective_provider_model

router = APIRouter(prefix="/api/copilot", tags=["copilot"])


class Message(BaseModel):
    role: str
    content: str


class CopilotRequest(BaseModel):
    messages: List[Message]


def _pipeline_context(db: Session, tenant_id: int) -> str:
    deals = db.query(models.Deal).filter(models.Deal.tenant_id == tenant_id).all()
    rows = [
        {
            "company": d.party.display_name if d.party else "",
            "deal": d.name,
            "value": d.value,
            "stage": d.stage,
        }
        for d in deals
    ]
    return (
        "You are the AI Copilot embedded inside Slate, an enterprise CRM. "
        "Speak like a sharp, concise sales-ops analyst, never a generic assistant. "
        "Reference specific deals or numbers from this data when relevant. "
        "Keep answers under 100 words unless asked for more detail.\n\n"
        f"Current pipeline (JSON): {rows}"
    )


@router.post("")
async def copilot(
    payload: CopilotRequest,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    provider, model = get_effective_provider_model(db, tenant)
    if not ai_providers.is_configured(provider):
        key_env = ai_providers.PROVIDERS[provider]["key_env"]
        raise HTTPException(500, f"{key_env} is not configured on the server")

    context = _pipeline_context(db, user.tenant_id)

    api_messages = []
    for i, m in enumerate(payload.messages):
        if i == len(payload.messages) - 1 and m.role == "user":
            api_messages.append({"role": "user", "content": f"{context}\n\nUser: {m.content}"})
        else:
            api_messages.append({"role": m.role, "content": m.content})

    try:
        text = await ai_providers.call_llm(provider, model, api_messages, 1000)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return {"reply": text}
