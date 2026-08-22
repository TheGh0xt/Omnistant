"""The three core workflows.

These are deliberately *deterministic* functions rather than free-form model
reasoning.  Each returns structured data plus a `speech` string.  The ADK agent
calls them as tools and may rephrase the speech, but the decisions — which item
is missing, when something was last seen — come from the event log, not from a
model's recollection.  That is the difference between an agent that remembers
and an agent that improvises.

Gemini is used where language genuinely is the task: narrating a day.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from typing import Any, Sequence

from google.genai import types

from tools.vision import VisionResult, scan_frame
from utils.cache import get_cache
from utils.config import get_config, tz
from utils.db import Observation, Routine, Store, get_store, normalize_subject
from utils.gemini import get_client
from utils.logger import get_logger

log = get_logger(__name__)

# Repeat sightings of one item inside this window are the same fact.
ITEM_DEDUPE_WINDOW_SECONDS = 120

# How much we trust each way of learning something.
METHOD_WEIGHT = {"visual": 1.0, "voice": 0.85, "manual": 0.8, "inferred": 0.55}

# A brand-new user has no history, so a "work" trip starts from a sensible guess
# and is corrected by the first few scans.
STARTER_ROUTINES: dict[str, list[str]] = {
    "work": ["phone", "wallet", "keys", "laptop", "airpods", "badge"],
    "office": ["phone", "wallet", "keys", "laptop", "airpods", "badge"],
    "gym": ["phone", "keys", "water bottle", "headphones", "towel"],
    "shopping": ["phone", "wallet", "keys", "tote bag"],
    "school": ["phone", "wallet", "keys", "laptop", "notebook"],
}
DEFAULT_ROUTINE_ITEMS = ["phone", "wallet", "keys"]


def _fmt_time(moment: datetime) -> str:
    """8:47 AM, in the user's local clock."""
    return moment.astimezone(tz()).strftime("%-I:%M %p")


def _humanize_list(names: Sequence[str]) -> str:
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


# Words whose conventional capitalisation is not just title case.
_BRAND_CASE = {"airpods": "AirPods", "iphone": "iPhone", "ipad": "iPad",
               "macbook": "MacBook", "id": "ID", "tv": "TV"}


def _display(name: str) -> str:
    """Present normalised keys back to the user readably ('airpods' -> 'AirPods')."""
    return " ".join(_BRAND_CASE.get(word, word) for word in (name or "").split())


def humanize_items(keys: Sequence[str]) -> str:
    """Render normalised item keys the way a person says them: 'AirPods and keys'."""
    return _humanize_list([_display(k) for k in keys])


# ===========================================================================
# Workflow 1 — Leave detection
# ===========================================================================
@dataclass
class MissingItem:
    item: str
    last_seen_at: str | None = None
    last_seen_location: str | None = None
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": _display(self.item),
            "last_seen_at": self.last_seen_at,
            "last_seen_location": self.last_seen_location,
            "hint": self.hint,
        }


@dataclass
class LeaveScanResult:
    routine_name: str
    expected: list[str] = field(default_factory=list)
    found: list[dict[str, Any]] = field(default_factory=list)
    missing: list[MissingItem] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    scene: str = ""
    speech: str = ""
    vision_available: bool = True
    routine_is_new: bool = False
    times_observed: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": "leave_detection",
            "routine": self.routine_name,
            "expected_items": [_display(i) for i in self.expected],
            "found_items": self.found,
            "missing_items": [m.to_dict() for m in self.missing],
            "unexpected_items": [_display(i) for i in self.extra],
            "scene": self.scene,
            "speech": self.speech,
            "vision_available": self.vision_available,
            "note": self.note,
            "routine_is_new": self.routine_is_new,
            "times_observed": self.times_observed,
        }


async def _resolve_routine(store: Store, user_id: str, destination: str) -> tuple[Routine, bool]:
    """Fetch the learned routine for a destination, seeding one if it is new."""
    name = normalize_subject(destination or "out") or "out"
    existing = await store.get_routine(user_id, name)
    if existing:
        return existing, False
    seeded = Routine(
        user_id=user_id,
        routine_name=name,
        expected_items=[normalize_subject(i) for i in STARTER_ROUTINES.get(name, DEFAULT_ROUTINE_ITEMS)],
        location_label=name,
        times_observed=0,
    )
    await store.upsert_routine(seeded)
    log.info("seeded new routine", extra={"routine": name, "items": seeded.expected_items})
    return seeded, True


async def _last_seen_hint(store: Store, user_id: str, item: str) -> MissingItem:
    """Fold Workflow 2 into Workflow 1: don't just say it's missing, say where it is."""
    history = await store.find_by_subject(user_id, item, limit=1)
    if not history:
        return MissingItem(item=item, hint="I have no record of where this is.")
    last = history[0]
    where = _where(last.location_label, str((last.content or {}).get("detail") or ""))
    return MissingItem(
        item=item,
        last_seen_at=last.observed_at.isoformat(),
        last_seen_location=last.location_label,
        hint=f"Last seen {_at(where)} at {_fmt_time(last.observed_at)}.",
    )


# Phrases that already carry their own preposition, so "at" must not be prepended.
_HAS_PREPOSITION = ("in ", "on ", "at ", "under ", "next to ", "by ", "behind ",
                    "inside ", "beside ", "near ")


def _at(where: str) -> str:
    """Prefix a location phrase with "at" only when it reads correctly.

    "at home" and "at the office" are right; "at in my coat" and "at somewhere"
    are not.
    """
    if where == "somewhere" or where.lower().startswith(_HAS_PREPOSITION):
        return where
    return f"at {where}"


def _where(location: str | None, detail: str | None) -> str:
    """Say the most useful thing we know about where something is.

    A sighting usually carries both a coarse location ("home") and a specific
    one ("on the kitchen counter"). The specific half is what saves the user a
    search, so it must not be dropped — but "home" still orients them when they
    are somewhere else, so keep both when they differ.
    """
    location = (location or "").strip()
    detail = (detail or "").strip()
    if location and detail and detail.lower() not in location.lower():
        return f"{location}, {detail}"
    return detail or location or "somewhere"


def _compose_leave_speech(result: LeaveScanResult, destination: str) -> str:
    dest = _display(destination) if destination else "out"
    if not result.vision_available:
        expected = _humanize_list([_display(i) for i in result.expected])
        reason = result.note or "I can't see your camera right now."
        return f"{reason} I can't check, but for {dest} you normally take {expected}."
    if not result.found and not result.missing:
        return f"I didn't recognise anything in that frame. Try pointing the camera at your things."

    missing_names = [_display(m.item) for m in result.missing]
    if not missing_names:
        got = _humanize_list([f["name"] for f in result.found])
        return f"You're good to go. I can see your {got}."

    lead = f"You're missing your {_humanize_list(missing_names)}."
    if result.times_observed >= 2:
        pronoun = "them" if len(missing_names) > 1 else _pronoun(result.missing[0].item)
        lead += f" You usually take {pronoun} to {dest}."
    hints = [m.hint for m in result.missing if m.hint and m.last_seen_at]
    if hints:
        lead += " " + " ".join(hints[:2])
    if result.found:
        lead += f" You've got your {_humanize_list([f['name'] for f in result.found])}."
    return lead


async def leave_detection(
    *,
    user_id: str,
    session_id: str,
    destination: str,
    frame_data_url: str | None = None,
    origin: str | None = None,
) -> LeaveScanResult:
    """Workflow 1: the user is about to leave — check they have their things.

    Scans the current camera frame, compares what is visible against the learned
    routine for this destination, and reports what is missing along with where it
    was last seen.
    """
    store = get_store()
    cache = get_cache()
    now = datetime.now(timezone.utc)
    origin = origin or "home"

    routine, is_new = await _resolve_routine(store, user_id, destination)
    expected = [normalize_subject(i) for i in routine.expected_items]

    frame = frame_data_url or await cache.get_frame(session_id)
    vision = await scan_frame(frame) if frame else VisionResult(
        available=False, note="No camera frame was captured for this scan."
    )

    found_keys = vision.keys
    # Substring match both ways so "airpods case" satisfies an expected "airpods".
    def _is_present(want: str) -> bool:
        return any(want in got or got in want for got in found_keys)

    missing_keys = [i for i in expected if not _is_present(i)]
    extra_keys = [
        i.key for i in vision.items if not any(i.key in e or e in i.key for e in expected)
    ]

    result = LeaveScanResult(
        routine_name=routine.routine_name,
        expected=expected,
        found=[i.to_dict() | {"name": _display(i.key)} for i in vision.items],
        missing=[await _last_seen_hint(store, user_id, k) for k in missing_keys],
        extra=extra_keys,
        scene=vision.scene,
        vision_available=vision.available,
        note=vision.note,
        routine_is_new=is_new,
        times_observed=routine.times_observed,
    )
    result.speech = _compose_leave_speech(result, destination)

    # --- persist what we learned ------------------------------------------
    observations = [
        Observation(
            user_id=user_id, observation_type="item", subject=item.key,
            content={"detail": item.detail, "scene": vision.scene, "source": "leave_scan"},
            observed_at=now, location_label=origin, confidence=item.confidence,
            verification_method="visual", session_id=session_id,
        )
        for item in vision.items
    ]
    observations.append(
        Observation(
            user_id=user_id, observation_type="activity",
            subject=f"leaving {origin} for {routine.routine_name}",
            content={
                "destination": routine.routine_name,
                "missing": missing_keys,
                "found": sorted(found_keys),
            },
            observed_at=now, location_label=origin, confidence=0.95,
            verification_method="voice", session_id=session_id,
        )
    )
    if vision.available:
        await store.add_observations(observations)
        await store.record_leave_scan(
            user_id=user_id, routine_name=routine.routine_name,
            found_items=sorted(found_keys), missing_items=missing_keys,
            extra_items=extra_keys,
            verdict="all present" if not missing_keys else f"missing {len(missing_keys)}",
            scanned_at=now,
        )
        await _refine_routine(store, user_id, routine)

        # A reminder is only useful if it arrives when you can still act on it.
        # Telling someone at 08:00 that they might forget their keys is a
        # forecast; telling them five minutes after they walked out is a rescue.
        if missing_keys:
            await store.enqueue_nudge(
                user_id=user_id,
                kind="left_without",
                due_at=now + timedelta(minutes=get_config().leave_nudge_delay_minutes),
                payload={
                    "routine": routine.routine_name,
                    "missing": missing_keys,
                    "origin": origin,
                    "scanned_at": now.isoformat(),
                },
            )

    log.info(
        "leave scan",
        extra={"routine": routine.routine_name, "missing": missing_keys,
               "found": sorted(found_keys), "user_id": user_id},
    )
    return result


async def _refine_routine(store: Store, user_id: str, routine: Routine) -> None:
    """Learn from repetition.

    An item the user has carried on most of their recent trips belongs in the
    routine even if nobody declared it.  This is what makes the second week
    better than the first.
    """
    scans = await store.recent_leave_scans(user_id, limit=8)
    relevant = [s for s in scans if s["routine_name"] == routine.routine_name]
    routine.times_observed = len(relevant)
    if len(relevant) >= 3:
        counts: dict[str, int] = {}
        for scan in relevant:
            for item in scan["found_items"]:
                counts[item] = counts.get(item, 0) + 1
        threshold = math.ceil(len(relevant) * 0.6)
        promoted = {i for i, n in counts.items() if n >= threshold}
        merged = sorted(set(routine.expected_items) | promoted)
        if merged != sorted(routine.expected_items):
            log.info(
                "routine refined",
                extra={"routine": routine.routine_name,
                       "added": sorted(promoted - set(routine.expected_items))},
            )
        routine.expected_items = merged
    await store.upsert_routine(routine)


# ===========================================================================
# Workflow 2 — Item recall
# ===========================================================================
@dataclass
class Sighting:
    at: datetime
    location: str | None
    detail: str
    confidence: float
    method: str
    time_str: str = ""

    def __post_init__(self) -> None:
        self.time_str = _fmt_time(self.at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "time": self.time_str,
            "location": self.location,
            "detail": self.detail,
            "confidence": round(self.confidence, 2),
            "method": self.method,
        }


@dataclass
class RecallResult:
    item: str
    found: bool
    sightings: list[Sighting] = field(default_factory=list)
    confidence: float = 0.0
    confidence_label: str = "none"
    speech: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": "item_recall",
            "item": _display(self.item),
            "found": self.found,
            "sightings": [s.to_dict() for s in self.sightings],
            "confidence": round(self.confidence, 2),
            "confidence_label": self.confidence_label,
            "speech": self.speech,
        }


def score_confidence(obs: Observation, now: datetime | None = None) -> float:
    """How much should we trust "it's still there"?

    Three things erode it: the original certainty, how it was verified, and how
    long ago.  Decay is exponential with a half-life of `recall_stale_after_hours`
    — after that long, a sighting is a lead, not an answer.
    """
    now = now or datetime.now(timezone.utc)
    half_life = max(0.5, get_config().recall_stale_after_hours)
    age_hours = max(0.0, (now - obs.observed_at).total_seconds() / 3600.0)
    recency = math.exp(-math.log(2) * age_hours / half_life)
    return max(0.0, min(1.0, obs.confidence * METHOD_WEIGHT.get(obs.verification_method, 0.6) * recency))


def _label(score: float) -> str:
    if score >= 0.7:
        return "high"
    if score >= 0.4:
        return "medium"
    if score > 0.0:
        return "low"
    return "none"


# Things people refer to in the plural even though they are one object.
_ALWAYS_PLURAL = {"airpods", "keys", "glasses", "headphones", "earbuds", "shoes"}


def _pronoun(item: str) -> str:
    return "them" if item in _ALWAYS_PLURAL or item.endswith("s") else "it"


def _compose_recall_speech(item: str, sightings: list[Sighting], label: str) -> str:
    name = _display(item)
    if not sightings:
        pronoun = _pronoun(item)
        return (
            f"I have no record of your {name}. If you tell me where you put {pronoun}, "
            f"or point the camera at {pronoun}, I'll remember from now on."
        )
    latest = sightings[0]
    where = _where(latest.location, latest.detail)
    how = {"visual": "I saw it", "voice": "you told me", "manual": "it was logged",
           "inferred": "I inferred it"}.get(latest.method, "it was recorded")

    line = f"Last confirmed {_at(where)} at {latest.time_str}"
    if label == "high":
        line += f" — {how}, so it should still be there."
    elif label == "medium":
        line += f" — {how}, but that was a while ago, so I'd double-check."
    else:
        line += f" — {how}, but that's old enough that I wouldn't rely on it."

    if len(sightings) > 1:
        prior = sightings[1]
        line += f" Before that, {_at(_where(prior.location, prior.detail))} at {prior.time_str}."
    return f"Your {name}: {line}"


async def item_recall(*, user_id: str, item: str, limit: int = 5) -> RecallResult:
    """Workflow 2: where was this thing last seen?

    Searches the observation log, ranks sightings newest-first, and attaches a
    confidence that decays with age and with how the sighting was verified.
    """
    store = get_store()
    subject = normalize_subject(item)
    now = datetime.now(timezone.utc)

    history = await store.find_by_subject(user_id, subject, limit=limit)
    sightings = [
        Sighting(
            at=o.observed_at,
            location=o.location_label,
            detail=str((o.content or {}).get("detail") or ""),
            confidence=score_confidence(o, now),
            method=o.verification_method,
        )
        for o in history
    ]
    top = sightings[0].confidence if sightings else 0.0
    result = RecallResult(
        item=subject, found=bool(sightings), sightings=sightings,
        confidence=top, confidence_label=_label(top),
    )
    result.speech = _compose_recall_speech(subject, sightings, result.confidence_label)
    log.info("item recall", extra={"item": subject, "hits": len(sightings), "confidence": round(top, 2)})
    return result


# ===========================================================================
# Workflow 3 — Daily timeline
# ===========================================================================
@dataclass
class TimelineEntry:
    at: datetime
    kind: str
    subject: str
    location: str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(), "time": _fmt_time(self.at), "kind": self.kind,
            "subject": self.subject, "location": self.location, "detail": self.detail,
        }


@dataclass
class TimelineResult:
    day: str
    entries: list[TimelineEntry] = field(default_factory=list)
    narrative: str = ""
    speech: str = ""
    answered_question: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": "daily_timeline",
            "day": self.day,
            "entries": [e.to_dict() for e in self.entries],
            "narrative": self.narrative,
            "speech": self.speech,
            "question": self.answered_question,
        }


_NARRATE_PROMPT = """These are timestamped observations from one person's day, in order.

{events}

Write a short second-person recap of their day: "You left home at 8:47, got to the
office around 9:20..." Two to four sentences, spoken aloud, no bullet points.

Hard rules:
- Use ONLY the observations above. Never invent an event, a place, or a time.
- Times must match the observations exactly.
- If the observations are sparse, say so plainly rather than padding.
{question_block}"""

_QUESTION_BLOCK = """
The person also asked: "{question}"
Answer that specifically from the observations. If the observations do not cover
it, say you have no record of that time rather than guessing."""


def _deterministic_narrative(entries: list[TimelineEntry]) -> str:
    if not entries:
        return "I don't have any observations logged for that day yet."
    parts = [f"{_fmt_time(e.at)}: {e.subject}{f' at {e.location}' if e.location else ''}" for e in entries]
    return "Here's what I have — " + "; ".join(parts) + "."


async def daily_timeline(
    *, user_id: str, day: Date | None = None, question: str | None = None
) -> TimelineResult:
    """Workflow 3: reconstruct the day from the observation log.

    Pulls the day's observations, then has Gemini narrate them.  The model is
    given the events and forbidden from inventing any — the timeline has to be
    what actually happened, not a plausible story about it.
    """
    store = get_store()
    local_tz = tz()
    day = day or datetime.now(local_tz).date()
    start_local = datetime.combine(day, time.min, tzinfo=local_tz)
    start = start_local.astimezone(timezone.utc)
    end = (start_local + timedelta(days=1)).astimezone(timezone.utc)

    observations = await store.observations_between(user_id, start, end)

    # Collapse repeat sightings of the SAME item: seeing the same laptop five
    # times in one scan is one fact, not five timeline entries. Two different
    # items seen a second apart are two facts and both must survive.
    entries: list[TimelineEntry] = []
    last_seen_at: dict[str, datetime] = {}
    for o in observations:
        if o.observation_type == "item":
            previous = last_seen_at.get(o.subject)
            if previous and (o.observed_at - previous).total_seconds() < ITEM_DEDUPE_WINDOW_SECONDS:
                continue
            last_seen_at[o.subject] = o.observed_at
        entries.append(
            TimelineEntry(
                at=o.observed_at, kind=o.observation_type, subject=o.subject,
                location=o.location_label, detail=str((o.content or {}).get("detail") or ""),
            )
        )

    result = TimelineResult(day=day.isoformat(), entries=entries, answered_question=question)

    if not entries:
        result.narrative = result.speech = (
            f"I don't have any observations logged for {day.isoformat()} yet. "
            "Once you start telling me where you're going, I'll be able to reconstruct your day."
        )
        return result

    client = get_client()
    if client is None:
        result.narrative = result.speech = _deterministic_narrative(entries)
        return result

    event_lines = "\n".join(
        f"- {_fmt_time(e.at)} | {e.kind} | {e.subject}"
        + (f" | at {e.location}" if e.location else "")
        + (f" | {e.detail}" if e.detail else "")
        for e in entries
    )
    prompt = _NARRATE_PROMPT.format(
        events=event_lines,
        question_block=_QUESTION_BLOCK.format(question=question) if question else "",
    )
    try:
        response = await client.aio.models.generate_content(
            model=get_config().model,
            contents=[prompt],
            config=types.GenerateContentConfig(temperature=0.2),
        )
        result.narrative = (response.text or "").strip() or _deterministic_narrative(entries)
    except Exception as exc:  # noqa: BLE001 - fall back rather than fail the turn
        log.warning("timeline narration failed", extra={"error": str(exc)})
        result.narrative = _deterministic_narrative(entries)

    result.speech = result.narrative
    log.info("timeline built", extra={"day": result.day, "entries": len(entries)})
    return result
