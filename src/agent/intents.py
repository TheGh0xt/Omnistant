"""Intent recognition.

Two-stage on purpose:

  1. A cheap deterministic pass over the utterance.  "where are my airpods" and
     "i'm heading to work" are unambiguous, and a regex answers in microseconds
     with no token spend and no chance of a model wobble mid-demo.
  2. Gemini for everything the regexes do not confidently claim.

The result is advisory: it seeds session state and tells the API whether a camera
frame is worth requesting.  The ADK agent still chooses which tool to call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from google.genai import types
from pydantic import BaseModel, Field

from utils.config import get_config
from utils.db import normalize_subject
from utils.gemini import get_client
from utils.logger import get_logger

log = get_logger(__name__)


class Intent(StrEnum):
    LEAVE_DETECTION = "leave_detection"
    ITEM_RECALL = "item_recall"
    DAILY_TIMELINE = "daily_timeline"
    LOG_OBSERVATION = "log_observation"
    UNKNOWN = "unknown"


@dataclass
class IntentResult:
    intent: Intent = Intent.UNKNOWN
    confidence: float = 0.0
    destination: str | None = None
    item: str | None = None
    time_reference: str | None = None
    source: str = "rules"
    slots: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_camera(self) -> bool:
        """Leave detection is the only workflow that wants a live frame."""
        return self.intent is Intent.LEAVE_DETECTION

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": str(self.intent),
            "confidence": self.confidence,
            "destination": self.destination,
            "item": self.item,
            "time_reference": self.time_reference,
            "source": self.source,
            "needs_camera": self.needs_camera,
        }


# --------------------------------------------------------------------------
# Stage 1 — rules
# --------------------------------------------------------------------------

# "I'm going to work", "heading out to the gym", "about to leave for the office"
_LEAVE = re.compile(
    r"\b(?:i(?:'m| am)?\s+)?(?:going|headed|heading|off|leaving|about to leave|on my way)\b"
    r"(?:\s+(?:out|now))?"
    r"(?:\s+(?:to|for)\s+(?P<dest>[\w\s'-]+?))?\s*[.!?]*$",
    re.IGNORECASE,
)
_LEAVE_BARE = re.compile(r"^\s*(?:i(?:'m| am)\s+)?(?:leaving|heading out|going out)\s*[.!?]*$", re.IGNORECASE)

# "where are my airpods", "where's my wallet", "did i leave my keys somewhere"
_RECALL = re.compile(
    r"\bwhere(?:'s|\s+is|\s+are|\s+did\s+i\s+(?:put|leave|last\s+see))?\b\s*"
    r"(?P<item>[\w\s'-]+?)\s*[.!?]*$",
    re.IGNORECASE,
)
_RECALL_ALT = re.compile(
    r"\b(?:have you seen|did i leave|do you know where)\b\s*(?P<item>[\w\s'-]+?)\s*[.!?]*$",
    re.IGNORECASE,
)

# "what did I do today", "what was I doing at 2pm", "recap my day"
_TIMELINE = re.compile(
    r"\b(?:what\s+(?:did|have)\s+i\s+(?:do|been\s+doing)|what\s+was\s+i\s+doing"
    r"|recap|summar(?:ise|ize)\s+my\s+day|my\s+day|timeline)\b",
    re.IGNORECASE,
)
_AT_TIME = re.compile(r"\bat\s+(?P<t>\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midnight)\b", re.IGNORECASE)

# "I put my keys on the table", "I'm at the office"
_LOG = re.compile(
    r"\b(?:i\s+(?:just\s+)?(?:put|left|dropped|placed|set)\s+(?P<item>[\w\s'-]+?)"
    r"\s+(?:on|in|at|by|under|next to)\s+(?P<place>[\w\s'-]+?))\s*[.!?]*$",
    re.IGNORECASE,
)
_AT_PLACE = re.compile(r"^\s*i(?:'m| am)\s+(?:at|in)\s+(?:the\s+)?(?P<place>[\w\s'-]+?)\s*[.!?]*$", re.IGNORECASE)

_DEST_STOPWORDS = {"out", "now", "there", "home now", ""}
_ITEM_STOPWORDS = {"i", "it", "that", "this", "everything", "am i", "we", "you", ""}

# "do you know where my badge is" captures "my badge is"; the copula is not part
# of the item's name.
_TRAILING_COPULA = re.compile(r"\s+(?:is|are|was|were|went|got|be)$", re.IGNORECASE)


def _clean_dest(raw: str | None) -> str | None:
    if not raw:
        return None
    dest = normalize_subject(raw)
    dest = re.sub(r"^(?:to|for)\s+", "", dest).strip()
    return None if dest in _DEST_STOPWORDS else dest


def _clean_item(raw: str | None) -> str:
    """Normalise a captured item name into something the store can match."""
    item = normalize_subject(raw or "")
    item = _TRAILING_COPULA.sub("", item).strip()
    return "" if item in _ITEM_STOPWORDS else item


def classify_rules(text: str) -> IntentResult:
    utterance = (text or "").strip()
    if not utterance:
        return IntentResult()

    if m := _LOG.search(utterance):
        item = _clean_item(m.group("item"))
        if item:
            return IntentResult(
                intent=Intent.LOG_OBSERVATION, confidence=0.9, item=item,
                slots={"place": normalize_subject(m.group("place"))},
            )
    if m := _AT_PLACE.match(utterance):
        return IntentResult(
            intent=Intent.LOG_OBSERVATION, confidence=0.85,
            slots={"place": normalize_subject(m.group("place")), "kind": "location"},
        )

    if _TIMELINE.search(utterance):
        t = _AT_TIME.search(utterance)
        return IntentResult(
            intent=Intent.DAILY_TIMELINE, confidence=0.9,
            time_reference=t.group("t").strip() if t else None,
        )

    for pattern in (_RECALL, _RECALL_ALT):
        if m := pattern.search(utterance):
            if item := _clean_item(m.group("item")):
                return IntentResult(intent=Intent.ITEM_RECALL, confidence=0.9, item=item)

    if _LEAVE_BARE.match(utterance):
        return IntentResult(intent=Intent.LEAVE_DETECTION, confidence=0.85)
    if m := _LEAVE.search(utterance):
        dest = _clean_dest(m.group("dest"))
        return IntentResult(
            intent=Intent.LEAVE_DETECTION,
            confidence=0.9 if dest else 0.7,
            destination=dest,
        )

    return IntentResult()


# --------------------------------------------------------------------------
# Stage 2 — Gemini
# --------------------------------------------------------------------------
class _Classification(BaseModel):
    intent: str = Field(
        description=(
            "One of: leave_detection (user is about to go somewhere), "
            "item_recall (asking where an object is), "
            "daily_timeline (asking what they did / what happened), "
            "log_observation (stating where they are or where they put something), "
            "unknown."
        )
    )
    confidence: float = Field(description="0.0-1.0")
    destination: str = Field(description="Where they are going, or empty string.")
    item: str = Field(description="The object being asked about or placed, or empty string.")
    time_reference: str = Field(description="Any time mentioned, e.g. '2pm', or empty string.")


_CLASSIFY_PROMPT = """Classify what this person wants from their personal context agent.

The agent helps someone with ADHD by (a) checking they have their things before
they leave, (b) remembering where objects were last seen, and (c) reconstructing
what they did during the day.

Utterance: {utterance}

Pick exactly one intent. Use "unknown" if it is small talk or genuinely unclear.
Leave a slot as an empty string when it is not mentioned."""


async def classify(text: str, *, rules_threshold: float = 0.7) -> IntentResult:
    """Classify an utterance, escalating to Gemini only when the rules are unsure."""
    rules = classify_rules(text)
    if rules.confidence >= rules_threshold:
        return rules

    client = get_client()
    if client is None:
        return rules  # no credentials: rules result (possibly UNKNOWN) is what we have

    try:
        response = await client.aio.models.generate_content(
            model=get_config().model,
            contents=[_CLASSIFY_PROMPT.format(utterance=text)],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_Classification,
                temperature=0.0,
            ),
        )
        parsed: _Classification | None = response.parsed
    except Exception as exc:  # noqa: BLE001 - never let classification break a turn
        log.warning("intent classification failed, using rules", extra={"error": str(exc)})
        return rules

    if parsed is None:
        return rules

    try:
        intent = Intent(parsed.intent.strip().lower())
    except ValueError:
        log.warning("model returned unknown intent label", extra={"label": parsed.intent})
        return rules

    return IntentResult(
        intent=intent,
        confidence=max(0.0, min(1.0, parsed.confidence)),
        destination=_clean_dest(parsed.destination) or None,
        item=_clean_item(parsed.item) or None,
        time_reference=parsed.time_reference.strip() or None,
        source="gemini",
    )
