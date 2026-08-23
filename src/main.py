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
from agent.watch import observe_tick
from agent.workflows import daily_timeline, humanize_items, item_recall, leave_detection
from tools.vision import ImageDecodeError, decode_data_url
from utils.cache import close_cache, get_cache, init_cache, new_session_id
from utils.config import get_config, tz
from utils.db import Routine, close_store, get_store, init_store, normalize_subject
from utils.errors import ModelQuotaError
from utils.logger import configure_logging, get_logger
from utils.notify import Notification, get_notifier, init_notifier

cfg = get_config()
configure_logging(cfg.log_level)
log = get_logger(__name__)

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_store()
    await init_cache()
    init_engine()
    init_notifier()
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


class ObserveRequest(BaseModel):
    """One tick of the watch loop."""

    session_id: str
    frame: str = Field(description="Camera still as a `data:image/jpeg;base64,...` URL.")
    user_id: str | None = None
    location: str | None = None
    spoken: str = Field(default="", description="What the user said while showing it, if anything.")


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
    """Liveness + dependency status. Cloud Run's health check hits this.

    Reports the backend actually in use, not just whether it answered. An
    in-process fallback always answers "healthy", so reporting a bare "up" would
    tell an operator that Redis is running when nothing was ever provisioned.
    """
    store = get_store()
    cache = get_cache()
    store_ok = await store.healthy()
    cache_ok = await cache.healthy()

    def state(healthy: bool, kind: str, real: str) -> str:
        if not healthy:
            return "down"
        return "up" if kind == real else f"fallback ({kind})"

    postgres = state(store_ok, store.kind, "postgres")
    redis = state(cache_ok, cache.kind, "redis")
    # A fallback is not a failure, but it is not full health either: nothing
    # written to an in-memory store survives the next cold start.
    degraded = not (store_ok and cache_ok) or "fallback" in postgres

    return {
        "status": "degraded" if degraded else "ok",
        "postgres": postgres,
        "redis": redis,
        "sessions": get_engine().session_backend,
        "gemini": "configured" if cfg.genai_available else "missing credentials",
        "notifications": "slack" if cfg.slack_webhook_url else "none",
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


@app.post("/api/observe")
async def api_observe(req: ObserveRequest) -> dict[str, Any]:
    """One frame of continuous watching.

    The client calls this only when the scene has actually changed, so this is
    not a fixed-rate poll — a still desk costs nothing. Returns what is visible,
    what just appeared, and what just left view.
    """
    try:
        decode_data_url(req.frame)
    except ImageDecodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    tick = await observe_tick(
        user_id=_user(req.user_id),
        session_id=req.session_id,
        frame_data_url=req.frame,
        location=req.location,
        spoken=req.spoken,
    )
    return tick.to_dict()


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
def _brief_window(typical_time: str | None) -> tuple[int, int] | None:
    """Minutes-since-midnight window in which the brief is useful."""
    if not typical_time:
        return None
    try:
        hh, mm = str(typical_time).split(":")[:2]
        departs = int(hh) * 60 + int(mm)
    except (ValueError, TypeError):
        return None
    return max(0, departs - BRIEF_LEAD_MINUTES), departs


# The brief is worth sending in the run-up to leaving, not at a fixed hour. This
# is how long before the learned departure time the window opens.
BRIEF_LEAD_MINUTES = 25


@app.post("/api/tasks/morning-brief", dependencies=[Depends(require_task_token)])
async def task_morning_brief(user_id: str | None = None, force: bool = False) -> dict[str, Any]:
    """Unprompted: shortly before you usually leave, say what today needs.

    Called on a cadence rather than at a fixed hour, and fires only inside a
    window ending at the departure time this routine has actually been observed
    to happen at. A brief pinned to 08:00 lands 45 minutes early for someone who
    leaves at 08:45, and lands after the fact on a day they leave at 07:30 —
    which makes it noise, and noise gets muted.

    `force=true` bypasses the window, for demos and manual checks.
    """
    uid = _user(user_id)
    store = get_store()
    routines = await store.list_routines(uid)
    primary = next((r for r in routines if r.routine_name in {"work", "office"}), None)
    if primary is None:
        return {"triggered": True, "message": "No work routine learned yet — nothing to brief on."}

    local_now = datetime.now(tz())
    if not force:
        window = _brief_window(primary.typical_time)
        if window is None:
            return {"triggered": False, "skipped": "no departure time learned yet"}
        opens, closes = window
        minutes_now = local_now.hour * 60 + local_now.minute
        if not (opens <= minutes_now <= closes):
            return {
                "triggered": False,
                "skipped": "outside the departure window",
                "window": f"{opens // 60:02d}:{opens % 60:02d}-{closes // 60:02d}:{closes % 60:02d}",
                "now": local_now.strftime("%H:%M"),
            }
        # Ticking every 15 minutes means the window is hit more than once; the
        # claim is what stops that becoming two notifications.
        if not await store.claim_daily_mark(uid, "morning-brief", local_now.date()):
            return {"triggered": False, "skipped": "already briefed today"}

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

    # Deliver it. A result that only exists in a JSON response is not an action
    # taken — nobody is watching this endpoint when it fires at 08:00.
    delivered = await get_notifier().send(
        Notification(
            title=f"Before you leave for {primary.routine_name}",
            body=message,
            facts=(
                [("Can't vouch for", ", ".join(u["item"] for u in unverified)),
                 ("Usually taken", ", ".join(primary.expected_items))]
                if unverified
                else [("Usually taken", ", ".join(primary.expected_items))]
            ),
            urgent=bool(unverified),
        )
    )

    log.info(
        "morning brief",
        extra={"user_id": uid, "unverified": [u["item"] for u in unverified], "delivered": delivered},
    )
    return {
        "triggered": True, "routine": primary.routine_name,
        "expected_items": primary.expected_items, "unverified": unverified,
        "message": message, "delivered": delivered,
    }


@app.post("/api/tasks/drain-nudges", dependencies=[Depends(require_task_token)])
async def task_drain_nudges() -> dict[str, Any]:
    """Deliver reminders that have come due.

    Cloud Run scales to zero, so a delayed reminder cannot be a sleeping task —
    it is a row with a due time, and this drains whatever has matured. Run it
    every few minutes.

    Each nudge is re-checked before it is sent. If the agent has seen the item
    since the scan that raised it, the reminder is cancelled rather than
    delivered: telling someone they forgot the keys they are holding is exactly
    the kind of noise that gets an assistant muted.
    """
    store = get_store()
    now = datetime.now(timezone.utc)
    due = await store.due_nudges(now)

    sent, cancelled = 0, 0
    for nudge in due:
        payload = nudge.get("payload") or {}
        scanned_at = payload.get("scanned_at")
        still_missing: list[str] = []

        # Where each thing was last seen, so the reminder is actionable. We are
        # already running this query to decide whether to send at all; throwing
        # the location away made the user turn back blind.
        last_seen: list[str] = []
        for item in payload.get("missing", []):
            recall = await item_recall(user_id=nudge["user_id"], item=item, limit=1)
            seen_since = (
                recall.found
                and scanned_at
                and recall.sightings[0].at.isoformat() > scanned_at
            )
            if seen_since:
                continue
            still_missing.append(item)
            if recall.found:
                sighting = recall.sightings[0]
                where = ", ".join(filter(None, [sighting.location, sighting.detail]))
                if where:
                    last_seen.append(f"{humanize_items([item])} — {where}, {sighting.time_str}")

        if not still_missing:
            await store.close_nudge(nudge["id"], sent=False)
            cancelled += 1
            continue

        names = humanize_items(still_missing)
        # Elapsed time comes from the scan, not the configured delay: the drain
        # runs on a cadence, so the real gap is whatever it actually was.
        try:
            elapsed = int((now - datetime.fromisoformat(scanned_at)).total_seconds() // 60)
        except (TypeError, ValueError):
            elapsed = int(cfg.leave_nudge_delay_minutes)
        when = "just now" if elapsed < 1 else f"about {elapsed} minute{'s' if elapsed != 1 else ''} ago"

        await get_notifier().send(
            Notification(
                title="You left without something",
                body=(
                    f"You headed to {payload.get('routine', 'out')} {when} without your "
                    f"{names}. Still close enough to turn back?"
                ),
                facts=(
                    [("Missing", names)]
                    + ([("Last seen", "\n".join(last_seen))] if last_seen else [])
                    + [("Left from", payload.get("origin", "home"))]
                ),
                urgent=True,
            )
        )
        await store.close_nudge(nudge["id"], sent=True)
        sent += 1

    if due:
        log.info("nudges drained", extra={"due": len(due), "sent": sent, "cancelled": cancelled})
    return {"triggered": True, "due": len(due), "sent": sent, "cancelled": cancelled}


@app.post("/api/tasks/evening-recap", dependencies=[Depends(require_task_token)])
async def task_evening_recap(user_id: str | None = None) -> dict[str, Any]:
    """Unprompted: at the end of the day, reconstruct it and surface loose ends."""
    uid = _user(user_id)
    timeline = await daily_timeline(user_id=uid)

    delivered = False
    if timeline.entries:
        # An empty day is not worth a notification; the log speaks for itself.
        places = [e.location for e in timeline.entries if e.location]
        delivered = await get_notifier().send(
            Notification(
                title="Your day",
                body=timeline.narrative,
                facts=[
                    ("Observations", str(len(timeline.entries))),
                    ("Places", ", ".join(dict.fromkeys(places)) or "none recorded"),
                ],
            )
        )

    log.info(
        "evening recap",
        extra={"user_id": uid, "entries": len(timeline.entries), "delivered": delivered},
    )
    return {"triggered": True, "delivered": delivered} | timeline.to_dict()


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
