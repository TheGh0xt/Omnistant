"""Continuous observation — the agent watching, rather than answering.

The conversational path is request/response: you ask, it looks once. That is not
what "notices what you own" means. This module is the other mode: the camera
stays on, frames arrive every few seconds, and the agent maintains a running
picture of what is in front of it.

What makes that affordable is the diff. A frame identical to the last one tells
us nothing, so the client never sends it (see `hasChanged` in camera.js), and the
server only writes an observation when the *set* of visible items changes. Point
the camera at a still desk for ten minutes and this costs nothing at all.

Two transitions matter, and they are what a person would notice too:

  * appeared  — something came into view. Worth remembering where it was.
  * left view — something that was there is gone. This is how "I watched you put
                your AirPods in your bag" actually gets recorded: not by seeing
                the bag, but by seeing the AirPods stop being on the desk.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from tools.vision import VisionResult, scan_frame
from utils.cache import get_cache
from utils.db import Observation, get_store
from utils.logger import get_logger

log = get_logger(__name__)

# An item must be absent this long before we call it "gone". One dropped frame,
# a hand passing over the desk, or a moment of blur should not read as "you put
# your wallet away".
GONE_AFTER_SECONDS = 12.0

# Don't re-log the same item over and over while it simply sits there.
RELOG_AFTER_SECONDS = 300.0

# Vision occasionally offers a low-confidence guess. Acting on those is how an
# agent starts telling people things that are not true.
MIN_CONFIDENCE = 0.55


@dataclass
class WatchTick:
    """One frame's worth of understanding."""

    scene: str = ""
    seen: list[dict[str, Any]] = field(default_factory=list)
    appeared: list[str] = field(default_factory=list)
    left_view: list[str] = field(default_factory=list)
    narration: str = ""
    logged: int = 0
    available: bool = True
    note: str = ""
    retry_after: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene": self.scene,
            "seen": self.seen,
            "appeared": self.appeared,
            "left_view": self.left_view,
            "narration": self.narration,
            "logged": self.logged,
            "available": self.available,
            "note": self.note,
            "retry_after": self.retry_after,
        }


def _display(name: str) -> str:
    from agent.workflows import _display as fmt

    return fmt(name)


def _humanize(names: list[str]) -> str:
    from agent.workflows import _humanize_list

    return _humanize_list([_display(n) for n in names])


async def _load_state(session_id: str) -> dict[str, Any]:
    raw = await get_cache().backend.get(f"watch:{session_id}")
    if not raw:
        return {"items": {}, "logged": {}}
    import json

    try:
        return json.loads(raw)
    except ValueError:
        return {"items": {}, "logged": {}}


async def _save_state(session_id: str, state: dict[str, Any]) -> None:
    import json

    await get_cache().backend.set(f"watch:{session_id}", json.dumps(state), 3600)


def _compose(appeared: list[str], left: list[str], seen: list[str], spoken: str) -> str:
    """What the agent says about this tick. Silence is a valid answer."""
    parts: list[str] = []
    if appeared:
        if spoken:
            parts.append(f"Noted — {_humanize(appeared)}.")
        else:
            parts.append(f"I can see your {_humanize(appeared)}.")
    if left:
        # Deliberately phrased as an observation, not a conclusion. The camera
        # saw it stop being there; it did not see where it went.
        parts.append(f"Your {_humanize(left)} went out of view — I've noted where it was last.")
    if not parts and seen:
        return ""  # nothing changed; say nothing rather than narrate the static world
    return " ".join(parts)


async def observe_tick(
    *,
    user_id: str,
    session_id: str,
    frame_data_url: str,
    location: str | None = None,
    spoken: str = "",
) -> WatchTick:
    """Process one frame from the watch loop."""
    now = datetime.now(timezone.utc)
    monotonic_now = time.time()
    location = location or "here"

    vision: VisionResult = await scan_frame(frame_data_url)
    if not vision.available:
        return WatchTick(available=False, note=vision.note)

    state = await _load_state(session_id)
    previous: dict[str, float] = state.get("items", {})
    last_logged: dict[str, float] = state.get("logged", {})

    confident = [i for i in vision.items if i.confidence >= MIN_CONFIDENCE]
    current = {i.key: i for i in confident}

    appeared = [k for k in current if k not in previous]
    left_view = [
        k for k, last_ts in previous.items()
        if k not in current and (monotonic_now - last_ts) >= GONE_AFTER_SECONDS
    ]

    # --- write to the log --------------------------------------------------
    store = get_store()
    to_write: list[Observation] = []
    for key, item in current.items():
        if monotonic_now - last_logged.get(key, 0.0) < RELOG_AFTER_SECONDS:
            continue
        to_write.append(
            Observation(
                user_id=user_id, observation_type="item", subject=key,
                content={"detail": item.detail, "scene": vision.scene,
                         "source": "watch", "said": spoken},
                observed_at=now, location_label=location,
                confidence=item.confidence, verification_method="visual",
                session_id=session_id,
            )
        )
        last_logged[key] = monotonic_now

    for key in left_view:
        to_write.append(
            Observation(
                user_id=user_id, observation_type="activity",
                subject=f"{key} left view",
                content={"item": key, "detail": "was visible, then was not",
                         "source": "watch"},
                observed_at=now, location_label=location,
                # Inferred: we saw it stop being there. We did not see where it went.
                confidence=0.7, verification_method="inferred",
                session_id=session_id,
            )
        )
        last_logged.pop(key, None)

    if to_write:
        await store.add_observations(to_write)

    # --- remember for the next tick ---------------------------------------
    state["items"] = {k: monotonic_now for k in current}
    for key, last_ts in previous.items():
        # Keep recently-departed items around so a one-frame blip doesn't count.
        if key not in current and (monotonic_now - last_ts) < GONE_AFTER_SECONDS:
            state["items"][key] = last_ts
    state["logged"] = last_logged
    await _save_state(session_id, state)

    tick = WatchTick(
        scene=vision.scene,
        seen=[i.to_dict() | {"name": _display(i.key)} for i in confident],
        appeared=[_display(k) for k in appeared],
        left_view=[_display(k) for k in left_view],
        narration=_compose(appeared, left_view, list(current), spoken),
        logged=len(to_write),
    )
    if appeared or left_view:
        log.info(
            "watch tick",
            extra={"appeared": appeared, "left_view": left_view,
                   "seen": sorted(current), "logged": len(to_write)},
        )
    return tick
