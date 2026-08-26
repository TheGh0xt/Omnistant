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
import re
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
from utils.location import get_current as current_location
from utils.location import set_current as set_current_location
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
    """8:47 AM, in the user's local clock. Time only — see `_fmt_when` for days."""
    return moment.astimezone(tz()).strftime("%-I:%M %p")


def _fmt_when(moment: datetime) -> str:
    """"8:47 AM" today, "yesterday at 8:47 AM", "Saturday at ...", "23 Aug at ...".

    A bare clock time is a lie by omission the moment a sighting is not from
    today. Reading "last seen at 11:09 AM" at 6:28 the next morning says the
    agent saw the thing five hours ago; it actually saw it the previous day, and
    the whole value of the answer is knowing which.

    Timeline entries deliberately keep the bare clock (`_fmt_time`): they are
    already grouped under a heading that names the day, so repeating it on every
    line is noise.
    """
    local = moment.astimezone(tz())
    clock = local.strftime("%-I:%M %p")
    days = (datetime.now(tz()).date() - local.date()).days
    if days <= 0:
        return clock
    if days == 1:
        return f"yesterday, {clock}"
    if days < 7:
        return f"{local.strftime('%A')}, {clock}"
    return f"{local.strftime('%-d %b')}, {clock}"


def _when_phrase(when: str) -> str:
    """Make a `_fmt_when` string read inside a sentence.

    "11:09 AM" needs an "at" in front of it; "yesterday, 11:09 AM" already reads
    as a time phrase and gets "at yesterday" if you add one. The display form is
    the one the UI shows in its details table, where a preposition would be
    wrong — so the two forms differ and this is where they meet.
    """
    if re.match(r"^\d{1,2}:\d{2}", when):   # "11:09 AM" — a bare clock time
        return f"at {when}"
    if when[:1].isdigit():                   # "14 Aug, 11:09 AM" — a date
        return f"on {when}"
    return when                              # "yesterday, ...", "Sunday, ..."


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
    # Not in the frame, but the log places them on you. A MissingItem here is a
    # last-seen record rather than a verdict — same shape, opposite conclusion.
    carried: list[MissingItem] = field(default_factory=list)
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
            "carried_items": [c.to_dict() for c in self.carried],
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
        hint=f"Last seen {_at(where)} {_when_phrase(_fmt_when(last.observed_at))}.",
    )


# Where a thing has to be for "you already have it" to be true.
#
# Deliberately about the body, and nothing looser. The camera watches a desk, so
# an item you have put on is invisible to it — that absence is exactly what the
# scan used to read as "missing". The log is the only thing that can tell "you
# picked it up" from "you left it behind", and only when it actually said so.
# A bag is not on this list: nothing can see inside a closed one, and a wrong
# all-clear costs more than a reminder you did not need.
# Matched as "preposition + body part" rather than a list of literal phrases,
# because the same fact reaches the log in two different voices: vision writes
# "in the person's right ear", and record_observation stores what you said as
# "in right ear". A fixed phrase list covered the first and silently missed the
# second, so telling the agent where a thing was did nothing for the very scan
# that was about to call it missing.
#
# The possessive and side words are enumerated rather than left open, which
# keeps "on the table near her hand" from reading as "in hand". Erring toward a
# reminder you did not need is the whole point.
_BODY_PART = r"ear|ears|wrist|wrists|neck|pocket|pockets|hand|hands|lanyard"
_ON_PERSON_RE = re.compile(
    r"\b(?:wearing|worn|on the person)\b"
    r"|\b(?:in|on|around)\s+"
    r"(?:(?:the\s+)?person's\s+|the\s+|a\s+|an\s+|my\s+|your\s+|his\s+|her\s+|their\s+|its\s+)?"
    r"(?:left\s+|right\s+|other\s+)?"
    rf"(?:{_BODY_PART})\b"
)


def _is_on_person(detail: str) -> bool:
    return bool(_ON_PERSON_RE.search((detail or "").lower()))


async def _carried_check(
    store: Store, user_id: str, item: str, now: datetime
) -> MissingItem | None:
    """Does the log already show this item on the user? Returns the record if so."""
    history = await store.find_by_subject(user_id, item, limit=1)
    if not history:
        return None
    last = history[0]
    detail = str((last.content or {}).get("detail") or "")
    if not _is_on_person(detail):
        return None
    # The same decay the recall workflow trusts. An on-person sighting is
    # evidence while it is fresh and a memory once it is not: what you wore on
    # Tuesday says nothing about what you have on now.
    if _label(score_confidence(last, now)) in {"none", "low"}:
        return None
    where = _where(last.location_label, detail)
    return MissingItem(
        item=item,
        last_seen_at=last.observed_at.isoformat(),
        last_seen_location=last.location_label,
        hint=f"You have it on you — last seen {_at(where)} {_when_phrase(_fmt_when(last.observed_at))}.",
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
    if not result.found and not result.missing and not result.carried:
        return f"I didn't recognise anything in that frame. Try pointing the camera at your things."

    # "I can see" is only true of the frame. Something the log places on you is
    # known, not seen, and the speech must not blur the two.
    seen_names = [f["name"] for f in result.found]
    carried_names = [_display(c.item) for c in result.carried]
    missing_names = [_display(m.item) for m in result.missing]
    if not missing_names:
        parts = []
        if seen_names:
            parts.append(f"I can see your {_humanize_list(seen_names)}")
        if carried_names:
            parts.append(f"you've already got your {_humanize_list(carried_names)}")
        # Not .capitalize(): it lowercases the rest, and "AirPods" is a name.
        tail = " and ".join(parts)
        return f"You're good to go. {tail[0].upper() + tail[1:]}."

    lead = f"You're missing your {_humanize_list(missing_names)}."
    if result.times_observed >= 2:
        pronoun = "them" if len(missing_names) > 1 else _pronoun(result.missing[0].item)
        lead += f" You usually take {pronoun} to {dest}."
    hints = [m.hint for m in result.missing if m.hint and m.last_seen_at]
    if hints:
        lead += " " + " ".join(hints[:2])
    if seen_names or carried_names:
        lead += f" You've got your {_humanize_list(seen_names + carried_names)}."
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
    # Where they are *now* — the scan happens before they walk out, so the items
    # it sees are still at the origin. Falls back to the configured home base.
    origin = origin or await current_location(session_id)

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

    # A scan with no usable frame did not look, which is not the same as looking
    # and seeing nothing. Deriving "missing" from an empty `found_keys` produced a
    # confident list of every expected item backed by no evidence — and it is what
    # hid a broken frame path for an entire demo: the `if vision.available` guard
    # below skipped the reminder while the UI still displayed six items missing,
    # so the failure looked exactly like a successful scan.
    missing_keys = [i for i in expected if not _is_present(i)] if vision.available else []
    extra_keys = [
        i.key for i in vision.items if not any(i.key in e or e in i.key for e in expected)
    ]

    # Something you are already wearing is not something you forgot. The frame
    # cannot show it — AirPods in your ear are behind the camera, not on the
    # desk — so anything the frame misses gets one more question asked of the
    # log before it is called missing.
    carried: list[MissingItem] = []
    not_on_them: list[str] = []
    for key in missing_keys:
        record = await _carried_check(store, user_id, key, now)
        if record is None:
            not_on_them.append(key)
        else:
            carried.append(record)
    missing_keys = not_on_them

    result = LeaveScanResult(
        routine_name=routine.routine_name,
        expected=expected,
        found=[i.to_dict() | {"name": _display(i.key)} for i in vision.items],
        missing=[await _last_seen_hint(store, user_id, k) for k in missing_keys],
        carried=carried,
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

        # The transition the user just announced in words. Everything observed
        # from here until the next announcement belongs to the destination, not
        # to the room the camera happens to be in.
        await set_current_location(session_id, routine.routine_name)

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


# Departures this far apart are different trips, not variations of one.
CLUSTER_SPAN_MINUTES = 90


def _typical_minutes(minutes: Sequence[int]) -> int | None:
    """When this trip actually happens: the middle of the tightest group.

    A plain median over every recent scan is dragged hours away by a single trip
    at another time of day — one evening departure among a week of 8am ones puts
    the "typical time" in the afternoon, and the brief window then sits on a time
    the user never leaves. So find the largest group of departures that fall
    within CLUSTER_SPAN_MINUTES of each other and take the middle of that.
    """
    if not minutes:
        return None
    best: list[int] = []
    for anchor in minutes:
        near = [m for m in minutes if abs(m - anchor) <= CLUSTER_SPAN_MINUTES]
        if len(near) > len(best):
            best = near
    return sorted(best)[len(best) // 2]


async def _refine_routine(store: Store, user_id: str, routine: Routine) -> None:
    """Learn from repetition.

    An item the user has carried on most of their recent trips belongs in the
    routine even if nobody declared it.  This is what makes the second week
    better than the first.
    """
    scans = await store.recent_leave_scans(user_id, limit=8)
    relevant = [s for s in scans if s["routine_name"] == routine.routine_name]
    routine.times_observed = len(relevant)

    # Learn when this trip actually happens. A seeded "08:45" that never updates
    # is a guess; the median of the last few departures is a fact, and it is what
    # decides when the pre-departure brief is worth sending.
    if len(relevant) >= 2:
        local = tz()
        minutes = sorted(
            s["scanned_at"].astimezone(local).hour * 60 + s["scanned_at"].astimezone(local).minute
            for s in relevant
            if s.get("scanned_at")
        )
        typical = _typical_minutes(minutes)
        if typical is not None:
            routine.typical_time = f"{typical // 60:02d}:{typical % 60:02d}"
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
        self.time_str = _fmt_when(self.at)

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

    line = f"Last confirmed {_at(where)} {_when_phrase(latest.time_str)}"
    if label == "high":
        line += f" — {how}, so it should still be there."
    elif label == "medium":
        line += f" — {how}, but that was a while ago, so I'd double-check."
    else:
        line += f" — {how}, but that's old enough that I wouldn't rely on it."

    if len(sightings) > 1:
        prior = sightings[1]
        line += f" Before that, {_at(_where(prior.location, prior.detail))} {_when_phrase(prior.time_str)}."
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
    *,
    user_id: str,
    day: Date | None = None,
    question: str | None = None,
    narrate: bool | None = None,
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

    # A question always needs the model — answering it from the log is the whole
    # point of asking. An unprompted recap does not.
    if narrate is None:
        narrate = get_config().recap_narrate or bool(question)
    client = get_client() if narrate else None
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
