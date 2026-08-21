"""The three core workflows."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agent import workflows as wf
from agent.workflows import daily_timeline, item_recall, leave_detection, score_confidence
from tests.conftest import SESSION, USER
from tools.vision import DetectedItem, VisionResult
from utils.db import utcnow


@pytest.fixture
def seen_items(monkeypatch):
    """Stub the vision model with a fixed set of detected objects."""
    def _install(names: list[str], scene: str = "entryway table"):
        result = VisionResult(
            items=[DetectedItem(name=n, confidence=0.92, detail="on the table") for n in names],
            scene=scene,
        )

        async def fake_scan(_data_url: str) -> VisionResult:
            return result

        monkeypatch.setattr(wf, "scan_frame", fake_scan)
        return result
    return _install


FRAME = "data:image/jpeg;base64,/9j/stub"


# --------------------------------------------------------------------------
# Workflow 1
# --------------------------------------------------------------------------
class TestLeaveDetection:
    async def test_reports_the_item_that_is_absent(self, store, cache, work_routine, seen_items):
        await store.upsert_routine(work_routine)
        seen_items(["phone", "wallet", "keys", "laptop"])  # no AirPods

        result = await leave_detection(
            user_id=USER, session_id=SESSION, destination="work", frame_data_url=FRAME
        )

        assert [m.item for m in result.missing] == ["airpods"]
        assert "AirPods" in result.speech
        assert "missing" in result.speech.lower()

    async def test_clears_the_user_when_everything_is_present(self, store, cache, work_routine, seen_items):
        await store.upsert_routine(work_routine)
        seen_items(["phone", "wallet", "keys", "laptop", "airpods"])

        result = await leave_detection(
            user_id=USER, session_id=SESSION, destination="work", frame_data_url=FRAME
        )

        assert result.missing == []
        assert "good to go" in result.speech.lower()

    async def test_matches_an_item_the_model_named_more_specifically(self, store, cache, work_routine, seen_items):
        """The routine says "airpods"; the model says "AirPods Pro case". Same thing."""
        await store.upsert_routine(work_routine)
        seen_items(["phone", "wallet", "keys", "laptop", "AirPods Pro case"])

        result = await leave_detection(
            user_id=USER, session_id=SESSION, destination="work", frame_data_url=FRAME
        )

        assert result.missing == []

    async def test_tells_the_user_where_the_missing_item_was_last_seen(
        self, store, cache, work_routine, seen_items, make_observation
    ):
        await store.upsert_routine(work_routine)
        await store.add_observation(
            make_observation("airpods", minutes_ago=30, location="kitchen counter")
        )
        seen_items(["phone", "wallet", "keys", "laptop"])

        result = await leave_detection(
            user_id=USER, session_id=SESSION, destination="work", frame_data_url=FRAME
        )

        assert result.missing[0].last_seen_location == "kitchen counter"
        assert "kitchen counter" in result.speech

    async def test_seeds_a_routine_for_an_unknown_destination(self, store, cache, seen_items):
        seen_items(["phone"])

        result = await leave_detection(
            user_id=USER, session_id=SESSION, destination="the gym", frame_data_url=FRAME
        )

        assert result.routine_is_new is True
        assert await store.get_routine(USER, "gym") is not None

    async def test_records_what_it_saw_so_recall_can_answer_later(
        self, store, cache, work_routine, seen_items
    ):
        await store.upsert_routine(work_routine)
        seen_items(["phone", "wallet", "keys", "laptop", "airpods"])

        await leave_detection(user_id=USER, session_id=SESSION, destination="work", frame_data_url=FRAME)
        recall = await item_recall(user_id=USER, item="laptop")

        assert recall.found is True
        assert recall.sightings[0].method == "visual"

    async def test_does_not_guess_when_there_is_no_camera_frame(self, store, cache, work_routine):
        await store.upsert_routine(work_routine)

        result = await leave_detection(user_id=USER, session_id=SESSION, destination="work")

        assert result.vision_available is False
        assert result.found == []
        # Nothing was seen, so nothing may be written to the log.
        assert await store.observations_between(
            USER, utcnow() - timedelta(hours=1), utcnow() + timedelta(hours=1)
        ) == []

    async def test_promotes_a_consistently_carried_item_into_the_routine(
        self, store, cache, work_routine, seen_items
    ):
        work_routine.expected_items = ["phone"]
        await store.upsert_routine(work_routine)
        seen_items(["phone", "water bottle"])

        for _ in range(3):
            await leave_detection(
                user_id=USER, session_id=SESSION, destination="work", frame_data_url=FRAME
            )

        refined = await store.get_routine(USER, "work")
        assert "water bottle" in refined.expected_items


# --------------------------------------------------------------------------
# Workflow 2
# --------------------------------------------------------------------------
class TestItemRecall:
    async def test_returns_the_most_recent_sighting_first(self, store, make_observation):
        await store.add_observation(make_observation("airpods", minutes_ago=200, location="home"))
        await store.add_observation(make_observation("airpods", minutes_ago=10, location="office"))

        result = await item_recall(user_id=USER, item="AirPods")

        assert [s.location for s in result.sightings] == ["office", "home"]

    async def test_says_it_has_no_record_rather_than_guessing(self, store):
        result = await item_recall(user_id=USER, item="umbrella")

        assert result.found is False
        assert result.confidence == 0.0
        assert "no record" in result.speech.lower()

    async def test_confidence_decays_with_age(self, store, make_observation):
        fresh = score_confidence(make_observation("keys", minutes_ago=1))
        stale = score_confidence(make_observation("keys", minutes_ago=60 * 24))

        assert fresh > stale
        assert stale < 0.2

    async def test_a_seen_item_outranks_a_merely_stated_one(self, make_observation):
        seen = score_confidence(make_observation("keys", minutes_ago=10, method="visual"))
        told = score_confidence(make_observation("keys", minutes_ago=10, method="voice"))
        guessed = score_confidence(make_observation("keys", minutes_ago=10, method="inferred"))

        assert seen > told > guessed

    async def test_an_old_sighting_is_reported_as_unreliable(self, store, make_observation):
        await store.add_observation(make_observation("keys", minutes_ago=60 * 20, location="office"))

        result = await item_recall(user_id=USER, item="keys")

        assert result.confidence_label in {"low", "none"}
        assert "wouldn't rely" in result.speech or "double-check" in result.speech

    async def test_normalises_how_the_user_says_it(self, store, make_observation):
        await store.add_observation(make_observation("airpods", minutes_ago=5))

        for phrasing in ("my AirPods", "AirPods?", "  airpods  "):
            assert (await item_recall(user_id=USER, item=phrasing)).found is True

    async def test_plural_items_read_naturally(self, store):
        result = await item_recall(user_id=USER, item="keys")
        assert "put them" in result.speech


# --------------------------------------------------------------------------
# Workflow 3
# --------------------------------------------------------------------------
class TestDailyTimeline:
    async def test_orders_the_day_chronologically(self, store, make_observation):
        await store.add_observation(make_observation("office", minutes_ago=60, kind="location"))
        await store.add_observation(make_observation("lunch", minutes_ago=30, kind="activity"))
        await store.add_observation(make_observation("home", minutes_ago=120, kind="location"))

        result = await daily_timeline(user_id=USER)

        assert [e.subject for e in result.entries] == ["home", "office", "lunch"]

    async def test_admits_an_empty_day_rather_than_inventing_one(self, store):
        result = await daily_timeline(user_id=USER)

        assert result.entries == []
        assert "don't have any observations" in result.speech

    async def test_falls_back_to_a_plain_list_without_a_model(self, store, make_observation):
        await store.add_observation(make_observation("office", minutes_ago=60, kind="location"))

        result = await daily_timeline(user_id=USER)

        assert "office" in result.speech
        assert len(result.entries) == 1


class TestWherePhrasing:
    """The specific half of a location is the half that saves a search."""

    async def test_keeps_both_the_coarse_and_specific_location(self, store, make_observation):
        await store.add_observation(
            make_observation("airpods", minutes_ago=5, location="home", detail="on the kitchen counter")
        )
        result = await item_recall(user_id=USER, item="airpods")

        assert "home, on the kitchen counter" in result.speech

    async def test_does_not_repeat_itself_when_they_overlap(self, store, make_observation):
        await store.add_observation(
            make_observation("keys", minutes_ago=5, location="hall table", detail="hall table")
        )
        result = await item_recall(user_id=USER, item="keys")

        assert result.speech.count("hall table") == 1

    async def test_falls_back_to_whichever_half_exists(self, store, make_observation):
        await store.add_observation(make_observation("wallet", minutes_ago=5, location="", detail="in my coat"))
        result = await item_recall(user_id=USER, item="wallet")

        assert "in my coat" in result.speech

    async def test_does_not_say_at_in_my_coat(self, store, make_observation):
        await store.add_observation(make_observation("wallet", minutes_ago=5, location="", detail="in my coat"))
        result = await item_recall(user_id=USER, item="wallet")

        assert "at in my coat" not in result.speech
        assert "in my coat" in result.speech

    async def test_says_at_home_not_home(self, store, make_observation):
        await store.add_observation(make_observation("keys", minutes_ago=5, location="home", detail=""))
        result = await item_recall(user_id=USER, item="keys")

        assert "at home" in result.speech
