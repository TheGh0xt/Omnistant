"""FastAPI service — the Cloud Run entry point.

Serves the web frontend and the agent API.  Two families of endpoints:

  * `/api/*`       — driven by a human in the browser.
  * `/api/tasks/*` — driven by Cloud Scheduler, with no human present. These are
                     what make the "agent" claim honest: it acts on its own.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent.engine import get_engine, init_engine
from agent.workflows import daily_timeline, item_recall, leave_detection
from tools.vision import ImageDecodeError, decode_data_url
from utils.cache import close_cache, get_cache, init_cache, new_session_id
from utils.config import get_config, tz
from utils.db import Routine, close_store, get_store, init_store, normalize_subject
from utils.errors import ModelQuotaError
from utils.logger import configure_logging, get_logger

cfg = get_config()
configure_logging(cfg.log_level)
log = get_logger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_store()
    await init_cache()
    init_engine()
    log.info("service ready", extra=cfg.report())
    yield
    await close_store()
    await close_cache()


app = FastAPI(
    title="Personal Context Agent",
    description="An agent that notices what you have, remembers where it was, and tells you before it matters.",
    version="1.0.0",
    lifespan=lifespan,
)

# The frontend is served from this same origin in production; CORS is open so the
# page can also be opened from a file:// or a separate dev server during a demo.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    text: str = Field(description="What the user said or typed.")
    session_id: str | None = None
    user_id: str | None = None
    frame: str | None = Field(default=None, description="Optional camera still as a data URL.")


class FrameRequest(BaseModel):
    session_id: str
    image: str = Field(description="Camera still as a `data:image/jpeg;base64,...` URL.")


class LeaveScanRequest(BaseModel):
    destination: str = "work"
    session_id: str | None = None
    user_id: str | None = None
    frame: str | None = None
    origin: str | None = None


class RoutineRequest(BaseModel):
    expected_items: list[str]
    typical_time: str | None = None
    user_id: str | None = None


def _user(explicit: str | None) -> str:
    return explicit or cfg.default_user_id


def require_task_token(x_task_token: str | None = Header(default=None)) -> None:
    """Guard the scheduler endpoints.

    Cloud Scheduler sends a shared secret. If TASK_TOKEN is unset we allow the
    call through (local dev) but say so loudly in the logs.
    """
    if not cfg.task_token:
        log.warning("task endpoint called with no TASK_TOKEN configured — open to anyone")
        return
    if x_task_token != cfg.task_token:
        raise HTTPException(status_code=401, detail="invalid or missing X-Task-Token")


# ---------------------------------------------------------------------------
# Health & meta
# ---------------------------------------------------------------------------
# Both spellings are served. `/healthz` is the Google convention (Borg, then
# Kubernetes; the trailing `z` exists so a probe endpoint cannot collide with an
# app's own `/health` route) and is what the Dockerfile HEALTHCHECK uses.
# `/health` is what a person types. Neither should 404.
@app.get("/health")
@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Liveness + dependency status. Cloud Run's health check hits this."""
    store_ok = await get_store().healthy()
    cache_ok = await get_cache().healthy()
    return {
        "status": "ok" if store_ok and cache_ok else "degraded",
        "postgres": "up" if store_ok else "down",
        "redis": "up" if cache_ok else "down",
        "gemini": "configured" if cfg.genai_available else "missing credentials",
        "model": cfg.model,
        "timezone": str(tz()),
    }


@app.get("/api/config")
async def client_config() -> dict[str, Any]:
    """What the frontend needs to know at boot."""
    return {
        "user_id": cfg.default_user_id,
        "session_id": new_session_id(),
        "model": cfg.model,
        "subsystems": cfg.report(),
    }


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------
@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    """One conversational turn. The agent decides which workflow to run."""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    if req.frame:
        try:
            decode_data_url(req.frame)
        except ImageDecodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    reply = await get_engine().handle(
        text=req.text,
        user_id=_user(req.user_id),
        session_id=req.session_id or new_session_id(),
        frame_data_url=req.frame,
    )
    return reply.to_dict()


@app.post("/api/frame")
async def upload_frame(req: FrameRequest) -> dict[str, Any]:
    """Stash the latest camera still so the next turn can scan it."""
    try:
        raw, mime = decode_data_url(req.image)
    except ImageDecodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await get_cache().put_frame(req.session_id, req.image)
    return {"stored": True, "bytes": len(raw), "mime": mime}


# ---------------------------------------------------------------------------
# Workflows, callable directly (used by the UI buttons and by tests)
# ---------------------------------------------------------------------------
@app.post("/api/leave-scan")
async def api_leave_scan(req: LeaveScanRequest) -> dict[str, Any]:
    """Workflow 1 — check for missing items before leaving."""
    session_id = req.session_id or new_session_id()
    if req.frame:
        try:
            decode_data_url(req.frame)
        except ImageDecodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = await leave_detection(
        user_id=_user(req.user_id),
        session_id=session_id,
        destination=req.destination,
        frame_data_url=req.frame,
        origin=req.origin,
    )
    return result.to_dict() | {"session_id": session_id}


@app.get("/api/recall")
async def api_recall(
    item: str = Query(description="The object to look for."),
    user_id: str | None = None,
) -> dict[str, Any]:
    """Workflow 2 — where was this last seen?"""
    if not item.strip():
        raise HTTPException(status_code=400, detail="item is required")
    result = await item_recall(user_id=_user(user_id), item=item)
    return result.to_dict()


@app.get("/api/timeline")
async def api_timeline(
    day: str = Query(default="", description="ISO date, or empty for today."),
    question: str = Query(default="", description="Optional question about the day."),
    user_id: str | None = None,
) -> dict[str, Any]:
    """Workflow 3 — reconstruct the day."""
    from tools.timeline_tools import _parse_day

    result = await daily_timeline(
        user_id=_user(user_id), day=_parse_day(day), question=question.strip() or None
    )
    return result.to_dict()


# ---------------------------------------------------------------------------
# State inspection — the UI shows these so the agent's memory is visible
# ---------------------------------------------------------------------------
@app.get("/api/observations")
async def api_observations(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    user_id: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    rows = await get_store().observations_between(_user(user_id), now - timedelta(hours=hours), now)
    return {
        "count": len(rows),
        "observations": [
            {
                "id": o.id, "type": o.observation_type, "subject": o.subject,
                "at": o.observed_at.isoformat(),
                "time": o.observed_at.astimezone(tz()).strftime("%-I:%M %p"),
                "location": o.location_label, "confidence": round(o.confidence, 2),
                "method": o.verification_method, "detail": (o.content or {}).get("detail", ""),
            }
            for o in reversed(rows)
        ],
    }


@app.get("/api/routines")
async def api_routines(user_id: str | None = None) -> dict[str, Any]:
    routines = await get_store().list_routines(_user(user_id))
    return {"routines": [r.to_dict() for r in routines]}


@app.put("/api/routines/{name}")
async def api_put_routine(name: str, req: RoutineRequest) -> dict[str, Any]:
    user_id = _user(req.user_id)
    store = get_store()
    key = normalize_subject(name)
    existing = await store.get_routine(user_id, key)
    routine = Routine(
        user_id=user_id, routine_name=key,
        expected_items=[normalize_subject(i) for i in req.expected_items if i.strip()],
        location_label=existing.location_label if existing else key,
        typical_time=req.typical_time or (existing.typical_time if existing else None),
        times_observed=existing.times_observed if existing else 0,
    )
    await store.upsert_routine(routine)
    return routine.to_dict()


@app.get("/api/session/{session_id}")
async def api_session(session_id: str, user_id: str | None = None) -> dict[str, Any]:
    state = await get_cache().get_session(session_id, _user(user_id))
    return state.to_dict()


# ---------------------------------------------------------------------------
# Autonomous triggers (Cloud Scheduler)
# ---------------------------------------------------------------------------
@app.post("/api/tasks/morning-brief", dependencies=[Depends(require_task_token)])
async def task_morning_brief(user_id: str | None = None) -> dict[str, Any]:
    """Unprompted: before the usual departure time, say what today needs.

    Runs with no user in the loop. Reports what the work routine expects and
    which of those things the agent has not seen recently enough to vouch for.
    """
    uid = _user(user_id)
    store = get_store()
    routines = await store.list_routines(uid)
    primary = next((r for r in routines if r.routine_name in {"work", "office"}), None)
    if primary is None:
        return {"triggered": True, "message": "No work routine learned yet — nothing to brief on."}

    unverified: list[dict[str, Any]] = []
    for item in primary.expected_items:
        recall = await item_recall(user_id=uid, item=item, limit=1)
        if recall.confidence_label in {"none", "low"}:
            unverified.append({"item": item, "confidence": recall.confidence_label})

    if unverified:
        names = ", ".join(u["item"] for u in unverified)
        message = f"Before you head to {primary.routine_name}: I can't currently vouch for your {names}."
    else:
        message = f"Everything you normally take to {primary.routine_name} was seen recently."

    log.info("morning brief", extra={"user_id": uid, "unverified": [u["item"] for u in unverified]})
    return {
        "triggered": True, "routine": primary.routine_name,
        "expected_items": primary.expected_items, "unverified": unverified, "message": message,
    }


@app.post("/api/tasks/evening-recap", dependencies=[Depends(require_task_token)])
async def task_evening_recap(user_id: str | None = None) -> dict[str, Any]:
    """Unprompted: at the end of the day, reconstruct it and surface loose ends."""
    uid = _user(user_id)
    timeline = await daily_timeline(user_id=uid)
    log.info("evening recap", extra={"user_id": uid, "entries": len(timeline.entries)})
    return {"triggered": True} | timeline.to_dict()


# ---------------------------------------------------------------------------
# Errors & static frontend
# ---------------------------------------------------------------------------
@app.exception_handler(ModelQuotaError)
async def quota_exceeded(request: Request, exc: ModelQuotaError) -> JSONResponse:
    """Over the model's rate limit. Temporary, and the client should say so."""
    headers = {"Retry-After": str(int(exc.retry_after or 30))}
    return JSONResponse(
        status_code=429,
        content={"detail": str(exc), "error": "quota_exceeded", "retry_after": exc.retry_after},
        headers=headers,
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error", extra={"path": request.url.path})
    return JSONResponse(status_code=500, content={"detail": "internal error", "path": request.url.path})


if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=cfg.port, reload=False)
