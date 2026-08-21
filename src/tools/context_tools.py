"""Context tools: observe the world, record what was seen, describe the present.

These are the functions the ADK agent is allowed to call.  Each one is a thin,
well-documented wrapper over a workflow or the store — the docstring *is* the
tool description Gemini sees, so it is written for the model as much as for us.

Convention: an empty string means "not supplied".  Gemini's function calling has
no clean notion of an omitted argument, so every parameter is a plain required
string and the tool decides what to do with a blank.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from google.adk.tools import ToolContext

from agent.workflows import leave_detection
from utils.cache import get_cache
from utils.db import Observation, Routine, get_store, normalize_subject
from utils.logger import get_logger

log = get_logger(__name__)


def _ids(tool_context: ToolContext) -> tuple[str, str]:
    """Pull the caller's identity out of ADK session state."""
    state = tool_context.state
    return state.get("user_id", ""), state.get("session_id", "")


async def check_before_leaving(destination: str, tool_context: ToolContext) -> dict[str, Any]:
    """Check whether the user has everything they need before they leave.

    Call this whenever the user says they are going somewhere, heading out, or
    leaving. It looks at the most recent camera frame, identifies what is
    actually visible, compares that against what they usually take to this
    destination, and reports anything missing along with where it was last seen.

    Args:
        destination: Where they are going, e.g. "work", "the gym", "shopping".
            Pass an empty string if they did not say.

    Returns:
        A dict with the expected items, what was found, what is missing (each
        with a last-seen hint), and a suggested spoken reply under "speech".
    """
    user_id, session_id = _ids(tool_context)
    state = tool_context.state
    result = await leave_detection(
        user_id=user_id,
        session_id=session_id,
        destination=destination or "out",
        origin=state.get("current_location") or "home",
    )
    state["current_intent"] = "leave_detection"
    return result.to_dict()


async def record_observation(
    kind: str, subject: str, detail: str, location: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Remember something the user just told you.

    Use this when the user states a fact worth recalling later: where they put an
    object ("I left my keys on the hall table"), where they are ("I'm at the
    office"), or what they are doing ("having lunch").

    Args:
        kind: One of "item", "location", or "activity".
        subject: What is being observed, e.g. "keys", "office", "lunch".
        detail: Any extra description, e.g. "on the hall table". May be empty.
        location: Where this happened, e.g. "home". May be empty.

    Returns:
        A dict confirming what was stored.
    """
    user_id, session_id = _ids(tool_context)
    kind = kind.strip().lower()
    if kind not in {"item", "location", "activity"}:
        return {"stored": False, "error": f"kind must be item, location or activity (got {kind!r})"}
    if not subject.strip():
        return {"stored": False, "error": "subject is required"}

    now = datetime.now(timezone.utc)
    observation = Observation(
        user_id=user_id,
        observation_type=kind,
        subject=subject,
        content={"detail": detail, "source": "user_statement"},
        observed_at=now,
        location_label=(location or detail or None) if kind == "item" else (location or subject),
        # The user told us directly: high confidence, but not eyes-on-it visual.
        confidence=0.9,
        verification_method="voice",
        session_id=session_id,
    )
    await get_store().add_observation(observation)

    cache = get_cache()
    await cache.note_last_seen(
        user_id, observation.subject,
        {"location": observation.location_label, "at": now.isoformat(),
         "confidence": 0.9, "method": "voice"},
    )
    if kind == "location":
        state = tool_context.state
        state["current_location"] = normalize_subject(subject)
        session = await cache.get_session(session_id, user_id)
        session.current_location = normalize_subject(subject)
        await cache.save_session(session)

    log.info("observation recorded", extra={"kind": kind, "subject": observation.subject})
    return {
        "stored": True,
        "kind": kind,
        "subject": observation.subject,
        "location": observation.location_label,
        "at": now.isoformat(),
    }


async def get_current_context(tool_context: ToolContext) -> dict[str, Any]:
    """Report what the agent currently believes about the user's situation.

    Use this when you need to know where the user is or what they were last
    doing before answering.

    Returns:
        A dict with the current location, the last intent, and whether a fresh
        camera frame is available to scan.
    """
    user_id, session_id = _ids(tool_context)
    cache = get_cache()
    session = await cache.get_session(session_id, user_id)
    frame = await cache.get_frame(session_id)
    return {
        "current_location": session.current_location,
        "current_intent": session.current_intent,
        "camera_frame_available": bool(frame),
        "recent_observations": session.active_observations[-5:],
        "learned_context": session.learned_context,
    }


async def list_known_routines(tool_context: ToolContext) -> dict[str, Any]:
    """List the destinations the agent has learned routines for.

    Use this to answer questions like "what do I usually take to work?".

    Returns:
        A dict mapping each routine name to its expected items and how many
        trips it has been observed over.
    """
    user_id, _ = _ids(tool_context)
    routines = await get_store().list_routines(user_id)
    return {
        "routines": [
            {
                "name": r.routine_name,
                "expected_items": r.expected_items,
                "times_observed": r.times_observed,
                "typical_time": r.typical_time,
            }
            for r in routines
        ]
    }


async def update_routine(destination: str, items: str, tool_context: ToolContext) -> dict[str, Any]:
    """Set the list of things the user takes to a destination.

    Use this when the user corrects the agent — "I don't take my laptop to the
    gym" or "add my badge to the work list".

    Args:
        destination: The routine to change, e.g. "work".
        items: The complete comma-separated list of items for this destination,
            e.g. "phone, wallet, keys, laptop".

    Returns:
        A dict with the routine's new expected items.
    """
    user_id, _ = _ids(tool_context)
    store = get_store()
    name = normalize_subject(destination)
    parsed = [normalize_subject(i) for i in items.split(",") if normalize_subject(i)]
    if not name:
        return {"updated": False, "error": "destination is required"}

    existing = await store.get_routine(user_id, name)
    routine = Routine(
        user_id=user_id, routine_name=name, expected_items=parsed,
        location_label=existing.location_label if existing else name,
        typical_time=existing.typical_time if existing else None,
        times_observed=existing.times_observed if existing else 0,
    )
    await store.upsert_routine(routine)
    log.info("routine updated by user", extra={"routine": name, "items": parsed})
    return {"updated": True, "routine": name, "expected_items": parsed}
