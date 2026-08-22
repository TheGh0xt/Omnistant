"""Continuous observation.

The watch loop writes to the same log the recall workflow reads, so a mistake
here becomes a confidently-wrong answer later. These tests pin the transitions.
"""

from __future__ import annotations

import pytest

from agent import watch as watch_module
from agent.watch import GONE_AFTER_SECONDS, observe_tick
from agent.workflows import item_recall
from tests.conftest import SESSION, USER
from tools.vision import DetectedItem, VisionResult

FRAME = "data:image/jpeg;base64,/9j/stub"


@pytest.fixture
def eyes(monkeypatch):
    """Drive what the camera 'sees' on each tick."""
    state = {"items": [], "scene": "desk"}

    async def fake_scan(_url: str) -> VisionResult:
        return VisionResult(
            items=[DetectedItem(name=n, confidence=c, detail="on the desk")
                   for n, c in state["items"]],
            scene=state["scene"],
        )

    monkeypatch.setattr(watch_module, "scan_frame", fake_scan)

    def see(*names, confidence: float = 0.9):
        state["items"] = [(n, confidence) for n in names]
    return see


@pytest.fixture
def clock(monkeypatch):
    """Control the watch loop's sense of time."""
    now = {"t": 1000.0}
    monkeypatch.setattr(watch_module.time, "time", lambda: now["t"])

    def advance(seconds: float):
        now["t"] += seconds
    return advance


async def tick(spoken: str = ""):
    return await observe_tick(
        user_id=USER, session_id=SESSION, frame_data_url=FRAME,
        location="desk", spoken=spoken,
    )


class TestSeeing:
    async def test_first_sighting_is_reported_and_logged(self, store, cache, eyes, clock):
        eyes("airpods", "phone")
        result = await tick()

        assert sorted(result.appeared) == ["AirPods", "phone"]
        assert result.logged == 2
        assert "AirPods" in result.narration

    async def test_a_static_scene_says_nothing_the_second_time(self, store, cache, eyes, clock):
        eyes("airpods")
        await tick()
        clock(3)
        result = await tick()

        assert result.appeared == []
        assert result.narration == ""      # narrating an unchanged world is noise
        assert result.logged == 0          # and re-logging it is worse

    async def test_low_confidence_guesses_are_ignored(self, store, cache, eyes, clock):
        eyes("airpods", confidence=0.3)
        result = await tick()

        assert result.appeared == []
        assert result.logged == 0

    async def test_what_it_sees_becomes_answerable(self, store, cache, eyes, clock):
        eyes("wallet")
        await tick()

        recall = await item_recall(user_id=USER, item="wallet")
        assert recall.found is True
        assert recall.sightings[0].method == "visual"


class TestLeavingView:
    async def test_an_item_that_vanishes_is_not_reported_immediately(self, store, cache, eyes, clock):
        """A hand passing over the desk is not 'you put your wallet away'."""
        eyes("airpods")
        await tick()

        eyes()                       # gone from this frame
        clock(2)
        result = await tick()

        assert result.left_view == []

    async def test_a_sustained_absence_is_recorded(self, store, cache, eyes, clock):
        eyes("airpods")
        await tick()

        eyes()
        clock(GONE_AFTER_SECONDS + 1)
        result = await tick()

        assert result.left_view == ["AirPods"]
        assert "out of view" in result.narration

    async def test_leaving_view_is_logged_as_inferred_not_visual(self, store, cache, eyes, clock):
        """We saw it stop being there. We did not see where it went."""
        eyes("airpods")
        await tick()
        eyes()
        clock(GONE_AFTER_SECONDS + 1)
        await tick()

        subjects = await store.distinct_subjects(USER, "activity")
        assert "airpods left view" in subjects

    async def test_an_item_that_comes_back_appears_again(self, store, cache, eyes, clock):
        eyes("airpods")
        await tick()
        eyes()
        clock(GONE_AFTER_SECONDS + 1)
        await tick()

        eyes("airpods")
        clock(2)
        result = await tick()

        assert result.appeared == ["AirPods"]


class TestDegradation:
    async def test_a_failed_scan_does_not_write_anything(self, store, cache, monkeypatch, clock):
        async def blind(_url: str) -> VisionResult:
            return VisionResult(available=False, note="Vision is rate-limited right now.")

        monkeypatch.setattr(watch_module, "scan_frame", blind)
        result = await tick()

        assert result.available is False
        assert result.logged == 0
        assert "rate-limited" in result.note
