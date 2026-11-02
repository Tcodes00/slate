import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from database import Base, SessionLocal, engine
import compliance
import workflow_engine
from events import redis_listener
from metrics import CONTENT_TYPE_LATEST, MetricsMiddleware, render_metrics
from plugins_loader import discover_and_mount
from production_guard import check_production_safety
from rate_limit import limiter
from routers import (
    agents, ai_studio, analytics, api_manager, audit, auth_router, automation, branding,
    compliance as compliance_router, copilot, deals, documents, erp, feature_flags, finance, hrm, inventory, kb, marketing,
    ml, model_endpoints, plugins, projects, rag, sales, support, workflows, ws,
)

check_production_safety()
Base.metadata.create_all(bind=engine)

METRICS_TOKEN = os.getenv("METRICS_TOKEN", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Real event-driven pub/sub: this is the one background subscriber
    # that turns Redis publishes (see events.py) into WebSocket pushes
    # for whichever clients are connected to this process. Started once,
    # for the life of the app; cancelled cleanly on shutdown rather than
    # left to die mid-read when the process exits.
    listener_task = asyncio.create_task(redis_listener())
    # Workflow engine: fires schedule triggers and reaps runs whose worker died (workflow_engine.py).
    scheduler_task = asyncio.create_task(workflow_engine.scheduler_loop())
    # Retention policies are enforced, not just recorded (compliance.py).
    retention_task = asyncio.create_task(compliance.retention_loop())
    yield
    for task in (retention_task, scheduler_task, listener_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Slate API", version="0.1.0", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(MetricsMiddleware)

ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(deals.router)
app.include_router(copilot.router)
app.include_router(finance.router)
app.include_router(analytics.router)
app.include_router(inventory.router)
app.include_router(sales.router)
app.include_router(hrm.router)
app.include_router(support.router)
app.include_router(erp.router)
app.include_router(documents.router)
app.include_router(projects.router)
app.include_router(marketing.router)
app.include_router(kb.router)
app.include_router(api_manager.router)
app.include_router(ai_studio.router)
app.include_router(model_endpoints.router)
app.include_router(rag.router)
app.include_router(ml.router)
app.include_router(compliance_router.router)
app.include_router(workflows.router)
app.include_router(workflows.hooks_router)
app.include_router(workflows.credentials_router)
app.include_router(automation.router)
app.include_router(agents.router)
app.include_router(audit.router)
app.include_router(feature_flags.router)
app.include_router(branding.router)
app.include_router(plugins.router)
app.include_router(ws.router)

# Plugin auto-discovery — scans backend/plugins/ and mounts each one's
# router under /api/plugins/{key}/. Synchronous and safe to run at import
# time: it's just include_router() calls, no I/O beyond local disk.
discover_and_mount(app)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics(request: Request):
    """Prometheus scrape target. Deliberately outside /api and outside
    the tenant JWT system — this is scraped by infrastructure, not
    browsed by a workspace user. Set METRICS_TOKEN to require a bearer
    token on it; unset (the default) leaves it open, matching how most
    Prometheus setups run inside a private network already."""
    if METRICS_TOKEN:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {METRICS_TOKEN}":
            raise HTTPException(401, "Unauthorized")
    return Response(content=render_metrics(SessionLocal), media_type=CONTENT_TYPE_LATEST)
