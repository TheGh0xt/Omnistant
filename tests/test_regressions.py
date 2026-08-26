"""Regressions for bugs found while building this.

Each test here corresponds to something that actually went wrong on a real run,
not a hypothetical.
"""

from __future__ import annotations

import importlib

import pytest

from agent.workflows import daily_timeline, item_recall
from tests.conftest import SESSION, USER


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


async def test_the_watch_loop_leaves_a_frame_the_leave_scan_can_use(store, cache, monkeypatch):
    """The watch loop is the only thing holding a camera frame. It must stash it.

    `/api/frame` was the endpoint whose whole job was caching the current frame,
    and the continuous-observation rewrite stopped the frontend calling it. The
    watch loop replaced it on screen but not in the cache, so `leave_detection`
    fell back to `cache.get_frame()` and got nothing — which made
    `vision.available` False, which skipped the one `enqueue_nudge` call in the
    codebase. The scan still announced every expected item as missing, so it
    looked right on camera while queueing no reminder at all.
    """
    from agent import watch as watch_module
    from agent.watch import observe_tick
    from tools.vision import DetectedItem, VisionResult

    async def fake_scan(_url: str) -> VisionResult:
        return VisionResult(
            items=[DetectedItem(name="phone", confidence=0.9, detail="on the desk")],
            scene="desk",
        )

    monkeypatch.setattr(watch_module, "scan_frame", fake_scan)

    frame = "data:image/jpeg;base64,/9j/stub"
    await observe_tick(user_id=USER, session_id=SESSION, frame_data_url=frame, location="desk")

    assert await cache.get_frame(SESSION) == frame


async def test_a_blind_scan_does_not_claim_everything_is_missing(store, cache, work_routine, monkeypatch):
    """No frame means we did not look. That is not the same as seeing nothing.

    With `found_keys` empty, every expected item fell out as "missing", so the
    agent confidently listed six things it had no evidence about — while the
    `if vision.available:` guard silently skipped the observations, the routine
    refinement and the reminder.
    """
    from agent.workflows import leave_detection

    await store.upsert_routine(work_routine)

    # No frame passed and nothing in the cache: the scan is blind.
    result = await leave_detection(user_id=USER, session_id=SESSION, destination="work")

    assert result.vision_available is False
    assert result.missing == [], "a blind scan has no evidence anyone is missing anything"
    assert result.note, "a blind scan must say why it could not answer"
    # Saying what they normally take is still useful; asserting six items are
    # missing is not. Only the second one is the bug.
    assert "missing" not in result.speech.lower()
