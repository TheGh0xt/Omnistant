"""The tool set handed to the ADK agent.

Order matters a little: Gemini reads these declarations in sequence, so the
three workflow tools come first and the supporting ones after.
"""

from __future__ import annotations

from .context_tools import (
    check_before_leaving,
    get_current_context,
    list_known_routines,
    record_observation,
    update_routine,
)
from .memory_tools import find_item, list_tracked_items
from .timeline_tools import summarize_day

AGENT_TOOLS = [
    # The three core workflows.
    check_before_leaving,
    find_item,
    summarize_day,
    # Supporting capabilities.
    record_observation,
    get_current_context,
    list_known_routines,
    list_tracked_items,
    update_routine,
]

__all__ = ["AGENT_TOOLS"] + [tool.__name__ for tool in AGENT_TOOLS]
