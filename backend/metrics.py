"""
Prometheus-format metrics at /metrics. Counters and histograms live in
prometheus_client's default process-local registry — with multiple
uvicorn workers, point Prometheus at each worker/port individually (the
normal way to run FastAPI behind Prometheus), rather than expecting one
process to aggregate another's counters.
"""

import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

REQUEST_COUNT = Counter(
    "slate_http_requests_total", "Total HTTP requests", ["method", "path", "status"]
)
REQUEST_LATENCY = Histogram(
    "slate_http_request_duration_seconds", "Request latency in seconds", ["method", "path"]
)
WS_CONNECTIONS = Gauge(
    "slate_websocket_connections", "Currently open WebSocket connections"
)
TENANTS_TOTAL = Gauge("slate_tenants_total", "Total tenants")
USERS_TOTAL = Gauge("slate_users_total", "Total users")
AGENT_HANDOFF_RUNS = Counter(
    "slate_agent_handoff_runs_total", "Multi-agent hand-off runs by terminal status", ["status"]
)


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/metrics":
            return await call_next(request)
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        # Route template, not the raw path, so /api/deals/17 and
        # /api/deals/42 count as one series instead of one per ID.
        route = request.scope.get("route")
        path = route.path if route else request.url.path
        REQUEST_COUNT.labels(method=request.method, path=path, status=response.status_code).inc()
        REQUEST_LATENCY.labels(method=request.method, path=path).observe(duration)
        return response


def render_metrics(session_local) -> bytes:
    db = session_local()
    try:
        import models
        TENANTS_TOTAL.set(db.query(models.Tenant).count())
        USERS_TOTAL.set(db.query(models.User).count())
    except Exception:
        pass
    finally:
        db.close()

    from ws_manager import manager
    WS_CONNECTIONS.set(manager.connection_count())

    return generate_latest()
