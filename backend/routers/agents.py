import asyncio
import json
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import ai_providers
import models
import schemas
from auth import get_current_user, require_role
from database import SessionLocal, get_db
from events import publish_event_async, spawn_background
from feature_flags import require_flag
from metrics import AGENT_HANDOFF_RUNS
from routers.ai_studio import get_effective_provider_model

router = APIRouter(prefix="/api/agents", tags=["agent-builder"])

MAX_HANDOFF_STEPS = 5
DEFAULT_EXECUTOR_PERSONA = (
    "You are a capable, concise execution agent completing one step of a larger plan devised by a "
    "planning agent. Do the step's work directly — don't ask clarifying questions, don't restate the "
    "instructions, and don't describe what you're about to do. Use the prior steps' output as context "
    "where relevant. Keep the response focused and under 250 words unless the step genuinely needs more."
)


def _agent_out(a: models.Agent) -> dict:
    return {"id": a.id, "name": a.name, "persona_prompt": a.persona_prompt, "enabled": a.enabled == "true", "created_at": a.created_at}


@router.get("", response_model=list[schemas.AgentOut])
def list_agents(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    agents = db.query(models.Agent).filter(models.Agent.tenant_id == user.tenant_id).all()
    return [_agent_out(a) for a in agents]


@router.post("", response_model=schemas.AgentOut)
def create_agent(payload: schemas.AgentCreate, db: Session = Depends(get_db), user: models.User = Depends(require_role("manager"))):
    agent = models.Agent(tenant_id=user.tenant_id, name=payload.name, persona_prompt=payload.persona_prompt, created_by_user_id=user.id)
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return _agent_out(agent)


@router.patch("/{agent_id}", response_model=schemas.AgentOut)
def toggle_agent(agent_id: int, payload: schemas.AgentUpdate, db: Session = Depends(get_db), user: models.User = Depends(require_role("manager"))):
    agent = db.query(models.Agent).filter(models.Agent.id == agent_id, models.Agent.tenant_id == user.tenant_id).first()
    if not agent:
        raise HTTPException(404, "Agent not found")
    agent.enabled = "true" if payload.enabled else "false"
    db.commit()
    db.refresh(agent)
    return _agent_out(agent)


@router.post("/{agent_id}/invoke", response_model=schemas.AgentInvokeOut)
async def invoke_agent(
    agent_id: int,
    payload: schemas.AgentInvoke,
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    """A real call to Claude with this agent's configured persona as the
    system framing — the same secure server-side pattern as the main
    Copilot (routers/copilot.py), just with a swappable persona instead
    of a fixed one. Multi-agent hand-off is the natural next layer on
    top of this working single-agent call, not a separate system."""
    agent = db.query(models.Agent).filter(models.Agent.id == agent_id, models.Agent.tenant_id == user.tenant_id).first()
    if not agent:
        raise HTTPException(404, "Agent not found")
    if agent.enabled != "true":
        raise HTTPException(400, "This agent is disabled")

    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    provider, model = get_effective_provider_model(db, tenant)
    if not ai_providers.is_configured(provider):
        raise HTTPException(500, f"{ai_providers.PROVIDERS[provider]['key_env']} is not configured on the server")

    try:
        text = await ai_providers.call_llm(
            provider, model, [{"role": "user", "content": f"{agent.persona_prompt}\n\nUser: {payload.message}"}], 800,
        )
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return {"reply": text}


# ---------------------------------------------------------------------------
# Multi-agent hand-off: a planner call breaks a goal into steps, then each
# step is executed in turn — by an existing Agent Builder persona where one
# fits, the built-in default executor otherwise — with every step handed the
# prior steps' output as context. Built on the exact same Anthropic call as
# invoke_agent above, not a separate orchestration engine.
# ---------------------------------------------------------------------------


def _run_out(run: models.AgentRun, steps: list) -> dict:
    return {
        "id": run.id, "goal": run.goal, "status": run.status,
        "plan_summary": run.plan_summary, "final_result": run.final_result, "error": run.error,
        "created_at": run.created_at, "completed_at": run.completed_at,
        "steps": [
            {
                "id": s.id, "step_number": s.step_number, "title": s.title,
                "assigned_agent_name": s.assigned_agent_name, "status": s.status,
                "output": s.output, "started_at": s.started_at, "completed_at": s.completed_at,
            }
            for s in steps
        ],
    }


def _step_event(run_id: int, s: models.AgentRunStep) -> dict:
    return {
        "run_id": run_id,
        "step": {
            "id": s.id, "step_number": s.step_number, "title": s.title,
            "assigned_agent_name": s.assigned_agent_name, "status": s.status, "output": s.output,
        },
    }


async def _call_llm(provider: str, model: str, persona: str, user_content: str, max_tokens: int) -> str:
    prefix = f"{persona}\n\n" if persona else ""
    return await ai_providers.call_llm(provider, model, [{"role": "user", "content": f"{prefix}{user_content}"}], max_tokens)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


async def _execute_handoff(run_id: int, tenant_id: int):
    """The actual planner -> executor pipeline. Runs as a background task
    (see spawn_background) so the POST that kicks it off returns the
    instant the run row exists; progress streams out over the tenant's
    WebSocket channel as each step changes state, and is always readable
    afterward via the GET endpoints below even if nobody was connected
    to watch it live."""
    db: Session = SessionLocal()
    try:
        run = db.query(models.AgentRun).filter(models.AgentRun.id == run_id).first()
        if not run:
            return
        tenant = db.query(models.Tenant).filter(models.Tenant.id == tenant_id).first()
        provider, model = get_effective_provider_model(db, tenant)
        agents = db.query(models.Agent).filter(models.Agent.tenant_id == tenant_id, models.Agent.enabled == "true").all()
        roster = "\n".join(f"- {a.name}: {a.persona_prompt[:160]}" for a in agents) or '(none configured — assign every step to "General Executor")'

        planner_prompt = (
            f"You are the planning stage of a multi-agent hand-off system. Break the goal below into 2-"
            f"{MAX_HANDOFF_STEPS} ordered, concrete steps. For each step, pick the single best-suited "
            'agent from the roster by name, or "General Executor" if none fit well.\n\n'
            f"Available agents:\n{roster}\n\nGoal: {run.goal}\n\n"
            "Respond with ONLY a JSON object, no preamble, no markdown fences, matching exactly this shape:\n"
            '{"plan_summary": "one sentence describing the overall approach", '
            '"steps": [{"title": "short step title", "instructions": "what this step should actually do", '
            '"agent": "agent name or General Executor"}]}'
        )

        try:
            raw = await _call_llm(provider, model, "", planner_prompt, 1200)
            plan = _extract_json(raw)
            steps_plan = plan["steps"][:MAX_HANDOFF_STEPS]
            if not steps_plan:
                raise ValueError("Planner returned zero steps")
        except Exception as e:
            run.status = "failed"
            run.error = f"Planning failed: {e}"
            run.completed_at = datetime.utcnow()
            db.commit()
            AGENT_HANDOFF_RUNS.labels(status="failed").inc()
            await publish_event_async(tenant_id, "agent_run.updated", _run_out(run, []))
            return

        run.status = "running"
        run.plan_summary = plan.get("plan_summary", "")
        by_name = {a.name.lower(): a for a in agents}
        step_rows = []
        for i, s in enumerate(steps_plan, start=1):
            agent_name = (s.get("agent") or "General Executor").strip()
            matched = by_name.get(agent_name.lower())
            row = models.AgentRunStep(
                tenant_id=tenant_id, run_id=run.id, step_number=i,
                title=s.get("title", f"Step {i}"), instructions=s.get("instructions", ""),
                assigned_agent_id=matched.id if matched else None,
                assigned_agent_name=matched.name if matched else "General Executor",
                status="pending",
            )
            db.add(row)
            step_rows.append(row)
        db.commit()
        for row in step_rows:
            db.refresh(row)
        await publish_event_async(tenant_id, "agent_run.updated", _run_out(run, step_rows))

        transcript = []  # hand-off context: what each prior step actually produced
        for row in step_rows:
            row.status = "running"
            row.started_at = datetime.utcnow()
            db.commit()
            await publish_event_async(tenant_id, "agent_run.step", _step_event(run.id, row))

            persona = by_name.get(row.assigned_agent_name.lower())
            persona_prompt = persona.persona_prompt if persona else DEFAULT_EXECUTOR_PERSONA
            history = "\n\n".join(f"Step {t['n']} ({t['title']}) produced:\n{t['output']}" for t in transcript) or "(no prior steps)"
            user_content = (
                f"Overall goal: {run.goal}\n\nPrior steps so far:\n{history}\n\n"
                f"Your step ({row.step_number} of {len(step_rows)}) — {row.title}:\n{row.instructions}"
            )

            try:
                output = await _call_llm(provider, model, persona_prompt, user_content, 800)
                row.status = "done"
                row.output = output
                transcript.append({"n": row.step_number, "title": row.title, "output": output})
            except Exception as e:
                row.status = "failed"
                row.output = str(e)
                row.completed_at = datetime.utcnow()
                db.commit()
                await publish_event_async(tenant_id, "agent_run.step", _step_event(run.id, row))
                run.status = "failed"
                run.error = f"Step {row.step_number} ('{row.title}') failed: {e}"
                run.completed_at = datetime.utcnow()
                db.commit()
                AGENT_HANDOFF_RUNS.labels(status="failed").inc()
                await publish_event_async(tenant_id, "agent_run.updated", _run_out(run, step_rows))
                return

            row.completed_at = datetime.utcnow()
            db.commit()
            await publish_event_async(tenant_id, "agent_run.step", _step_event(run.id, row))

        synthesis_prompt = (
            f"Overall goal: {run.goal}\n\n"
            + "\n\n".join(f"Step {t['n']} ({t['title']}):\n{t['output']}" for t in transcript)
            + "\n\nIn under 150 words, synthesize the above into a single final answer to the overall goal."
        )
        try:
            run.final_result = await _call_llm(provider, model, "", synthesis_prompt, 500)
        except Exception:
            run.final_result = transcript[-1]["output"] if transcript else None

        run.status = "completed"
        run.completed_at = datetime.utcnow()
        db.commit()
        db.refresh(run)
        AGENT_HANDOFF_RUNS.labels(status="completed").inc()
        await publish_event_async(tenant_id, "agent_run.updated", _run_out(run, step_rows))
    finally:
        db.close()


@router.post("/handoff", response_model=schemas.AgentRunOut)
async def start_handoff(
    payload: schemas.AgentRunCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(require_flag("multi_agent_handoff")),
):
    tenant = db.query(models.Tenant).filter(models.Tenant.id == user.tenant_id).first()
    provider, _model = get_effective_provider_model(db, tenant)
    if not ai_providers.is_configured(provider):
        raise HTTPException(500, f"{ai_providers.PROVIDERS[provider]['key_env']} is not configured on the server")
    if not payload.goal.strip():
        raise HTTPException(400, "A goal is required")

    run = models.AgentRun(tenant_id=user.tenant_id, goal=payload.goal.strip(), status="planning", created_by_user_id=user.id)
    db.add(run)
    db.commit()
    db.refresh(run)

    spawn_background(_execute_handoff(run.id, user.tenant_id))
    return _run_out(run, [])


@router.get("/handoff", response_model=list[schemas.AgentRunOut])
def list_handoffs(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    runs = (
        db.query(models.AgentRun).filter(models.AgentRun.tenant_id == user.tenant_id)
        .order_by(models.AgentRun.created_at.desc()).limit(20).all()
    )
    out = []
    for r in runs:
        steps = db.query(models.AgentRunStep).filter(models.AgentRunStep.run_id == r.id).order_by(models.AgentRunStep.step_number).all()
        out.append(_run_out(r, steps))
    return out


@router.get("/handoff/{run_id}", response_model=schemas.AgentRunOut)
def get_handoff(run_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    run = db.query(models.AgentRun).filter(models.AgentRun.id == run_id, models.AgentRun.tenant_id == user.tenant_id).first()
    if not run:
        raise HTTPException(404, "Run not found")
    steps = db.query(models.AgentRunStep).filter(models.AgentRunStep.run_id == run.id).order_by(models.AgentRunStep.step_number).all()
    return _run_out(run, steps)
