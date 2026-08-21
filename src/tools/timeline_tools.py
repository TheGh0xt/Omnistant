"""Timeline tools: reconstruct a day from the observation log."""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime, timedelta
from typing import Any

from google.adk.tools import ToolContext

from agent.workflows import daily_timeline
from utils.config import tz
from utils.logger import get_logger

log = get_logger(__name__)


def _parse_day(raw: str) -> Date:
    """Accept 'today', 'yesterday' or an ISO date; default to today."""
    text = (raw or "").strip().lower()
    today = datetime.now(tz()).date()
    if text in {"", "today"}:
        return today
    if text == "yesterday":
        return today - timedelta(days=1)
    try:
        return Date.fromisoformat(text)
    except ValueError:
        log.warning("unparseable day, defaulting to today", extra={"raw": raw})
        return today


async def summarize_day(day: str, question: str, tool_context: ToolContext) -> dict[str, Any]:
    """Reconstruct what the user did on a given day.

    Call this for "what did I do today?", "recap my day", or a specific question
    about a moment in it such as "what was I doing at 2pm?".

    The narrative is built only from logged observations. If the log is thin, say
    so — do not fill the gaps with plausible-sounding invention.

    Args:
        day: "today", "yesterday", or an ISO date like "2026-08-21". Empty means
            today.
        question: A specific question about the day, e.g. "what was I doing at
            2pm?". Empty for a general recap.

    Returns:
        A dict with the ordered timeline entries and a narrative under "speech".
    """
    user_id = tool_context.state.get("user_id", "")
    result = await daily_timeline(
        user_id=user_id, day=_parse_day(day), question=question.strip() or None
    )
    return result.to_dict()
