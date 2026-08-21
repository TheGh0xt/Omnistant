"""Regressions for bugs found while building this.

Each test here corresponds to something that actually went wrong on a real run,
not a hypothetical.
"""

from __future__ import annotations

import importlib

import pytest

from agent.workflows import daily_timeline, item_recall
from tests.conftest import USER


async def test_recall_ignores_items_named_in_a_leave_scans_missing_list(store, make_observation):
    """A leave scan records which items were MISSING. Recall must never read that
    list as evidence the item was seen.

    The original query matched `content::text ILIKE '%term%'`, so asking "where
    are my keys?" returned the leave-scan row — reporting a sighting at the exact
    moment we established the keys were nowhere to be found.
    """
    await store.add_observation(
        make_observation(
            "leaving home for work",
            kind="activity",
            method="voice",
            content={"destination": "work", "missing": ["keys", "wallet"], "found": []},
        )
    )

    result = await item_recall(user_id=USER, item="keys")

    assert result.found is False
    assert result.sightings == []
    assert "no record" in result.speech.lower()


async def test_recall_still_finds_genuine_sightings(store, make_observation):
    await store.add_observation(make_observation("keys", minutes_ago=5, location="hall table"))
    result = await item_recall(user_id=USER, item="keys")

    assert result.found is True
    assert result.sightings[0].location == "hall table"


async def test_timeline_keeps_distinct_items_seen_moments_apart(store, make_observation):
    """Two different items observed seconds apart are two facts.

    The original dedupe collapsed any `item` row following another `item` row
    within 120s, which silently deleted every item but the first in a scan.
    """
    await store.add_observation(make_observation("airpods", minutes_ago=1.0, location="kitchen"))
    await store.add_observation(make_observation("wallet", minutes_ago=0.9, location="hallway"))
    await store.add_observation(make_observation("laptop", minutes_ago=0.8, location="desk"))

    result = await daily_timeline(user_id=USER)

    assert [e.subject for e in result.entries] == ["airpods", "wallet", "laptop"]


async def test_timeline_collapses_repeat_sightings_of_the_same_item(store, make_observation):
    for offset in (1.0, 0.9, 0.8):
        await store.add_observation(make_observation("laptop", minutes_ago=offset, location="desk"))

    result = await daily_timeline(user_id=USER)

    assert [e.subject for e in result.entries] == ["laptop"]


def test_no_circular_imports_regardless_of_entry_point():
    """`agent.workflows` imports `tools.vision`, and `tools.context_tools` imports
    `agent.workflows`. Importing either side first must work.
    """
    for module in ("agent.workflows", "tools.registry", "tools.vision", "agent.engine", "main"):
        importlib.import_module(module)
