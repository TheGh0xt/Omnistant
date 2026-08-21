"""Memory tools: retrieve what was observed about a thing."""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext

from agent.workflows import item_recall
from utils.db import get_store
from utils.logger import get_logger

log = get_logger(__name__)


async def find_item(item_name: str, tool_context: ToolContext) -> dict[str, Any]:
    """Find where an object was last seen.

    Call this whenever the user asks where something is — "where are my
    AirPods?", "have you seen my keys?", "did I leave my wallet at home?".

    The answer comes with a confidence level that decays with age: a sighting
    from ten minutes ago is reliable, one from yesterday is a lead. Pass that
    nuance on to the user rather than stating an old sighting as fact.

    Args:
        item_name: The object to look for, e.g. "AirPods", "keys", "wallet".

    Returns:
        A dict with every recorded sighting newest-first, a confidence score and
        label, and a suggested spoken reply under "speech".
    """
    user_id = tool_context.state.get("user_id", "")
    if not item_name.strip():
        return {"found": False, "error": "item_name is required"}
    result = await item_recall(user_id=user_id, item=item_name)
    return result.to_dict()


async def list_tracked_items(tool_context: ToolContext) -> dict[str, Any]:
    """List every object the agent has ever observed for this user.

    Useful for answering "what do you know about?" or when the user's phrasing
    for an item does not match anything and you want to offer near misses.

    Returns:
        A dict with the item names, most frequently seen first.
    """
    user_id = tool_context.state.get("user_id", "")
    items = await get_store().distinct_subjects(user_id, "item")
    return {"items": items, "count": len(items)}
