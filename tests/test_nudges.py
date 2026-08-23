"""Delayed leave reminders.

A fixed 08:00 brief assumes a fixed departure. The useful moment is a few
minutes after you actually walk out — late enough that you have gone, early
enough that turning back is cheap.

The risk in a delayed reminder is that it arrives stale. These tests pin the
re-check that prevents it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent import workflows as wf
from agent.workflows import leave_detection
from tests.conftest import SESSION, USER
from tools.vision import DetectedItem, VisionResult

FRAME = "data:image/jpeg;base64,/9j/stub"


@pytest.fixture
def seen_items(monkeypatch):
    def _install(names: list[str]):
        result = VisionResult(
            items=[DetectedItem(name=n, confidence=0.92, detail="on the table") for n in names],
            scene="entryway",
        )

        async def fake_scan(_url: str) -> VisionResult:
            return result

        monkeypatch.setattr(wf, "scan_frame", fake_scan)
    return _install


async def test_a_missing_item_queues_a_delayed_reminder(store, cache, work_routine, seen_items):
    await store.upsert_routine(work_routine)
    seen_items(["phone", "wallet", "keys", "laptop"])        # no AirPods

    before = datetime.now(timezone.utc)
    await leave_detection(user_id=USER, session_id=SESSION, destination="work", frame_data_url=FRAME)

    queued = await store.due_nudges(before + timedelta(hours=1))
    assert len(queued) == 1
    assert queued[0]["kind"] == "left_without"
    assert queued[0]["payload"]["missing"] == ["airpods"]


async def test_the_reminder_is_not_due_immediately(store, cache, work_routine, seen_items):
    """Firing at the moment of the scan is just the scan again."""
    await store.upsert_routine(work_routine)
    seen_items(["phone"])

    await leave_detection(user_id=USER, session_id=SESSION, destination="work", frame_data_url=FRAME)

    assert await store.due_nudges(datetime.now(timezone.utc)) == []


async def test_nothing_missing_queues_nothing(store, cache, work_routine, seen_items):
    await store.upsert_routine(work_routine)
    seen_items(["phone", "wallet", "keys", "laptop", "airpods"])

    await leave_detection(user_id=USER, session_id=SESSION, destination="work", frame_data_url=FRAME)

    assert await store.due_nudges(datetime.now(timezone.utc) + timedelta(hours=1)) == []


async def test_a_delivered_reminder_is_not_delivered_twice(store, cache):
    now = datetime.now(timezone.utc)
    nudge_id = await store.enqueue_nudge(USER, "left_without", now - timedelta(minutes=1), {"missing": ["keys"]})

    await store.close_nudge(nudge_id, sent=True)

    assert await store.due_nudges(now) == []


async def test_a_cancelled_reminder_is_not_delivered(store, cache):
    now = datetime.now(timezone.utc)
    nudge_id = await store.enqueue_nudge(USER, "left_without", now - timedelta(minutes=1), {"missing": ["keys"]})

    await store.close_nudge(nudge_id, sent=False)

    assert await store.due_nudges(now) == []
