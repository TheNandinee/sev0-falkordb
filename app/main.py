import asyncio
import json
import queue
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import __version__
from . import agents, evals, remediation
from . import graph_ops as G
from . import queries as Q
from . import seed as S
from .config import settings
from .db import graph_name, ping, run
from .observability import event, metrics, setup_logging
from .security import auth_enabled, limit_investigations, resolve_tenant, verify_signature
from .simulator import simulator
from .telemetry import hub

STATIC = Path(__file__).resolve().parent.parent / "static"
QUIET = ("/api/alerts", "/api/graph", "/api/incidents", "/api/simulator", "/metrics", "/healthz", "/readyz")


class AccessLog:
    # Pure ASGI middleware (safe with streaming): request id, latency metrics, structured access log.
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = uuid.uuid4().hex[:12]
        started = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message["headers"] = list(message.get("headers", [])) + [(b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            ms = (time.perf_counter() - started) * 1000
            route = getattr(scope.get("route"), "path", scope.get("path", ""))
            metrics.inc("sev0_http_requests_total", method=scope.get("method", ""), route=route, status=status["code"])
            metrics.observe("sev0_http_ms", ms, route=route)
            if status["code"] >= 400 or not scope.get("path", "").startswith(QUIET):
                event("http", rid=rid, method=scope.get("method"), path=scope.get("path"),
                      status=status["code"], ms=round(ms, 1))


@asynccontextmanager
async def lifespan(app):
    setup_logging(settings.log_level)
    for _ in range(30):
        try:
            ping()
            break
        except Exception:
            await asyncio.sleep(1)
    try:
        if not G.has_data():
            S.seed()
            event("graph.seeded", graph=graph_name())
    except Exception as e:
        event("graph.seed_failed", error=str(e))
    hub.start()
    if settings.simulator_enabled:
        simulator.start()
    event("sev0.started", version=__version__, live=settings.simulator_enabled)
    yield
    simulator.stop()
    hub.stop()


app = FastAPI(title="Sev0", version=__version__, lifespan=lifespan,
              description="Graph-native on-call agent on FalkorDB")
app.add_middleware(AccessLog)


@app.exception_handler(PermissionError)
async def _forbidden(request, exc):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(remediation.ActionStateError)
async def _conflict(request, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


def _live_for(tenant):
    return simulator.running and (tenant in (None, settings.default_tenant))


# ---------------- infra ----------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True, "version": __version__}


@app.get("/readyz", include_in_schema=False)
def readyz():
    try:
        ping()
        return {"ready": True}
    except Exception as e:
        return JSONResponse(status_code=503, content={"ready": False, "error": str(e)})


@app.get("/metrics", include_in_schema=False)
def prometheus():
    return PlainTextResponse(metrics.render(), media_type="text/plain; version=0.0.4")


@app.get("/api/health")
def health(tenant: Optional[str] = Depends(resolve_tenant)):
    try:
        ping()
        db_ok = True
    except Exception:
        db_ok = False
    return {"version": __version__, "falkordb": db_ok, "graph": graph_name(tenant), "auth": auth_enabled(),
            "llm": "groq" if settings.groq_api_key else "planner",
            "model": settings.groq_model if settings.groq_api_key else None,
            "live": _live_for(tenant)}


# ---------------- graph ----------------
@app.post("/api/seed")
def seed(tenant: Optional[str] = Depends(resolve_tenant)):
    return S.seed(tenant)


@app.get("/api/graph")
def graph(tenant: Optional[str] = Depends(resolve_tenant)):
    return {"nodes": run(Q.SERVICE_NODES, tenant=tenant, readonly=True),
            "edges": run(Q.SERVICE_EDGES_V2, tenant=tenant, readonly=True)}


@app.get("/api/topology")
def topology(at: Optional[int] = None, tenant: Optional[str] = Depends(resolve_tenant)):
    ts = at or int(time.time())
    return {"at": ts, "edges": run(Q.TOPOLOGY_AT, {"at": ts}, tenant=tenant, readonly=True)}


@app.get("/api/alerts")
def alerts(tenant: Optional[str] = Depends(resolve_tenant)):
    return G.get_active_alerts(tenant)


@app.get("/api/blast/{service}")
def blast(service: str, tenant: Optional[str] = Depends(resolve_tenant)):
    return G.get_blast_radius(service, tenant)


@app.get("/api/critical")
def critical(tenant: Optional[str] = Depends(resolve_tenant)):
    return G.criticality(tenant=tenant)


# ---------------- outages ----------------
@app.get("/api/scenarios")
def scenarios():
    return {k: v["label"] for k, v in S.SCENARIOS.items()}


@app.post("/api/simulate/{name}")
def simulate(name: str, tenant: Optional[str] = Depends(resolve_tenant)):
    if name not in S.SCENARIOS:
        raise HTTPException(404, f"unknown scenario {name}")
    if _live_for(tenant):
        return {**simulator.inject(name), "mode": "live",
                "note": "Fault injected into live traffic. Alerts appear as the detector sees them (about 10 to 30 s)."}
    return {**S.simulate(name, tenant), "mode": "scripted"}


@app.post("/api/reset")
def reset(tenant: Optional[str] = Depends(resolve_tenant)):
    if _live_for(tenant):
        simulator.clear()
        return {"reset": True, "mode": "live", "note": "Faults cleared. Telemetry recovers in about 30 s."}
    return S.reset_outage(tenant)


@app.get("/api/simulator")
def simulator_status(tenant: Optional[str] = Depends(resolve_tenant)):
    return {**simulator.status(), "telemetry": hub.status(tenant)}


@app.post("/api/simulator/start")
def simulator_start(tenant: Optional[str] = Depends(resolve_tenant)):
    simulator.start()
    return simulator.status()


@app.post("/api/simulator/stop")
def simulator_stop(tenant: Optional[str] = Depends(resolve_tenant)):
    simulator.stop()
    return {"running": False}


# ---------------- ingestion ----------------
class Span(BaseModel):
    service: str = Field(min_length=1, max_length=200)
    span_id: Optional[str] = Field(default=None, max_length=64)
    parent_span_id: Optional[str] = Field(default=None, max_length=64)
    parent_service: Optional[str] = Field(default=None, max_length=200)
    duration_ms: float = Field(ge=0)
    error: bool = False


class SpanBatch(BaseModel):
    spans: List[Span] = Field(default_factory=list, max_length=20000)


@app.post("/api/ingest/spans")
def ingest_spans(batch: SpanBatch, tenant: Optional[str] = Depends(resolve_tenant)):
    accepted = hub.store(tenant).ingest([s.model_dump() for s in batch.spans])
    return {"accepted": accepted}


class DeployEvent(BaseModel):
    service: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=100)
    summary: str = Field(default="", max_length=500)
    author: str = Field(default="unknown", max_length=64)
    risky: bool = False


@app.post("/api/webhooks/deploy")
async def deploy_webhook(request: Request, tenant: Optional[str] = Depends(resolve_tenant)):
    body = await request.body()
    if not verify_signature(settings.webhook_secret, body, request.headers.get("x-sev0-signature")):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        ev = DeployEvent.model_validate_json(body)
    except Exception as e:
        raise HTTPException(422, f"Invalid payload: {e}")
    dep_id = f"DEP-{ev.service}-{int(time.time() * 1000)}"
    rows = await run_in_threadpool(run, Q.CREATE_DEPLOY_WEBHOOK, {
        "service": ev.service, "author": ev.author, "id": dep_id, "version": ev.version,
        "summary": ev.summary, "at": int(time.time()), "risky": int(ev.risky)}, tenant)
    if not rows:
        raise HTTPException(404, f"Unknown service {ev.service}")
    event("deploy.received", service=ev.service, version=ev.version, author=ev.author)
    return {"id": dep_id}


# ---------------- agents ----------------
@app.post("/api/investigate")
def investigate(tenant: Optional[str] = Depends(limit_investigations)):
    return agents.run_investigation(tenant)


@app.get("/api/investigate/stream")
def investigate_stream(tenant: Optional[str] = Depends(limit_investigations)):
    events = queue.Queue()

    def work():
        try:
            result = agents.run_investigation(tenant, on_step=lambda step: events.put(("step", step)))
            events.put(("failure", result) if result.get("error") else ("result", result))
        except Exception as e:
            event("investigation.failed", error=str(e))
            events.put(("failure", {"error": str(e)}))
        finally:
            events.put(None)

    threading.Thread(target=work, daemon=True, name="sev0-investigation").start()

    def stream():
        yield ": connected\n\n"
        while True:
            item = events.get()
            if item is None:
                break
            kind, payload = item
            yield f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------- incidents and remediation ----------------
@app.get("/api/incidents")
def incidents(tenant: Optional[str] = Depends(resolve_tenant)):
    return G.list_incidents(tenant)


@app.get("/api/incidents/{inc_id}/trace")
def trace(inc_id: str, tenant: Optional[str] = Depends(resolve_tenant)):
    return G.incident_trace(inc_id, tenant)


@app.post("/api/incidents/{inc_id}/resolve")
def resolve(inc_id: str, tenant: Optional[str] = Depends(resolve_tenant)):
    return G.resolve_incident(inc_id, tenant)


@app.get("/api/incidents/{inc_id}/actions")
def actions(inc_id: str, tenant: Optional[str] = Depends(resolve_tenant)):
    return remediation.list_actions(inc_id, tenant)


class Approval(BaseModel):
    handle: str = Field(min_length=1, max_length=64)


@app.post("/api/actions/{action_id}/approve")
def approve(action_id: str, body: Approval, tenant: Optional[str] = Depends(resolve_tenant)):
    return remediation.approve(action_id, body.handle.lstrip("@"), tenant)


@app.post("/api/actions/{action_id}/execute")
def execute(action_id: str, tenant: Optional[str] = Depends(resolve_tenant)):
    return remediation.execute(action_id, tenant, simulator if _live_for(tenant) else None)


# ---------------- evals ----------------
@app.post("/api/evals/run")
def run_evals(n: int = 100, seed: int = 7, tenant: Optional[str] = Depends(resolve_tenant)):
    return evals.run_evals(n=max(10, min(n, 500)), seed_value=seed)
