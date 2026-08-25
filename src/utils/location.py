"""Where the user currently is, as a label worth reading.

There is no GPS and no geofencing here — that needs a native app, and the
roadmap says so. What there *is* is stated intent: the leave-detection workflow
already hears "I'm heading to work", and that is a location transition told to
us in words. It was being thrown away.

Before this module every observation was logged at `"here"`, which is what the
camera knows and nothing a person can use. "Places: here" in the evening recap
is a row of output that costs a line and answers nothing.

The label is session state, not a fact about the world: it holds until the next
transition is announced, and it says "Unknown location" rather than guessing when
nothing has ever been announced and no default is configured.
"""

from __future__ import annotations

from .cache import get_cache
from .config import get_config
from .logger import get_logger

log = get_logger(__name__)

# Long enough to outlive a day's use, short enough that a stale label from last
# week never resurfaces on a session id that gets reused.
LOCATION_TTL_SECONDS = 86_400

# Said when nothing has been announced and no home base is configured. It is the
# honest answer, and it is more useful than a vague word: "Unknown location"
# tells you the agent has not been told, "here" does not.
UNKNOWN = "Unknown location"


def _key(session_id: str) -> str:
    return f"location:{session_id}"


def default_label() -> str:
    """The resting label when no transition has been announced.

    Configurable because "Home" is only the right baseline for someone whose day
    starts at home. Set DEFAULT_LOCATION_LABEL="" to opt out of guessing at all.
    """
    configured = (get_config().default_location_label or "").strip()
    return configured or UNKNOWN


def normalise(label: str | None) -> str:
    """Capitalise a spoken destination for display, touching only the first letter.

    "work" -> "Work". Deliberately not `.title()`, which would turn "NYC office"
    into "Nyc Office" — an acronym the user typed correctly is not ours to fix.
    """
    text = (label or "").strip()
    if not text:
        return ""
    return text[0].upper() + text[1:] if text[0].islower() else text


async def set_current(session_id: str, label: str) -> str:
    """Record an announced transition. Returns the label actually stored."""
    stored = normalise(label)
    if not stored:
        return await get_current(session_id)
    await get_cache().backend.set(_key(session_id), stored, LOCATION_TTL_SECONDS)
    log.info("location label set", extra={"session_id": session_id, "label": stored})
    return stored


async def get_current(session_id: str) -> str:
    """The label to tag new observations with.

    Falls back to the configured home base, then to UNKNOWN. Never "here".
    """
    if session_id:
        stored = await get_cache().backend.get(_key(session_id))
        if stored:
            return stored
    return default_label()
