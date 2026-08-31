"""Regressions for bugs found while building this.

Each test here corresponds to something that actually went wrong on a real run,
not a hypothetical.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

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


# The demo notification said "Missing: airpods" and then, two lines below,
# "AirPods — Home, in the person's right ear". One message, contradicting itself.
ON_PERSON = "in the person's right ear"
STUB_FRAME = "data:image/jpeg;base64,/9j/stub"


@pytest.fixture
def blind_camera(monkeypatch):
    """A camera pointed at a desk that holds none of the expected items."""
    from agent import workflows as wf
    from tools.vision import VisionResult

    async def sees_nothing(_url: str):
        return VisionResult(items=[], scene="an empty desk")

    monkeypatch.setattr(wf, "scan_frame", sees_nothing)


async def _scan(destination: str = "work"):
    from agent.workflows import leave_detection

    return await leave_detection(
        user_id=USER, session_id=SESSION, destination=destination, frame_data_url=STUB_FRAME
    )


async def test_an_item_on_your_person_is_not_missing(
    store, cache, work_routine, blind_camera, make_observation
):
    """AirPods in your ear are not missing, and the log already knows it.

    The scan compared the routine against what the camera can see *right now*,
    so anything you had already put on fell out as missing — while the very same
    notification quoted the sighting that proved you were wearing it.
    """
    await store.upsert_routine(work_routine)
    await store.add_observation(make_observation("airpods", detail=ON_PERSON, minutes_ago=50))

    result = await _scan()

    assert "airpods" not in [m.item for m in result.missing]
    assert "airpods" in [c.item for c in result.carried]


async def test_a_stale_on_person_sighting_is_not_proof_you_have_it(
    store, cache, work_routine, blind_camera, make_observation
):
    """Worn three days ago says nothing about this morning."""
    await store.upsert_routine(work_routine)
    await store.add_observation(
        make_observation("airpods", detail=ON_PERSON, minutes_ago=3 * 24 * 60)
    )

    result = await _scan()

    assert "airpods" in [m.item for m in result.missing]
    assert result.carried == []


async def test_an_item_left_on_the_desk_is_still_missing(
    store, cache, work_routine, blind_camera, make_observation
):
    """The check is "on you", not "seen recently". A desk is not a pocket."""
    await store.upsert_routine(work_routine)
    await store.add_observation(
        make_observation("wallet", detail="on the right side of the desk", minutes_ago=5)
    )

    result = await _scan()

    assert "wallet" in [m.item for m in result.missing]
    assert result.carried == []


async def test_the_reminder_does_not_name_what_you_are_wearing(
    store, cache, work_routine, blind_camera, make_observation
):
    """The queued nudge is what reaches Slack, so it must agree with the scan."""
    await store.upsert_routine(work_routine)
    await store.add_observation(make_observation("airpods", detail=ON_PERSON, minutes_ago=50))

    await _scan()

    queued = await store.due_nudges(datetime.now(timezone.utc) + timedelta(hours=1))
    assert len(queued) == 1
    assert "airpods" not in queued[0]["payload"]["missing"]


@pytest.mark.parametrize(
    "detail",
    [
        "in the person's right ear",   # how vision describes it
        "in right ear",                # how record_observation stores what you said
        "in my right ear",
        "in ear",
        "in his jacket",               # not a body part, but see the negative cases
        "on the wrist",
        "around my neck",
        "in a pocket",
        "wearing them",
    ],
)
def test_on_person_phrasings_agree_across_both_voices(detail):
    from agent.workflows import _is_on_person

    # "in his jacket" is the one entry here that must NOT count -- kept in the
    # same list so the boundary stays visible next to what does.
    expected = detail != "in his jacket"
    assert _is_on_person(detail) is expected, detail


@pytest.mark.parametrize(
    "detail",
    [
        "on the right side of the desk",
        "at the bottom left corner of the desk",
        "on the table near her hand",   # a hand is mentioned; the item is not in it
        "in the bag on the desk",
        "on the kitchen counter",
        "",
    ],
)
def test_a_thing_put_down_is_never_read_as_a_thing_worn(detail):
    from agent.workflows import _is_on_person

    assert _is_on_person(detail) is False, detail


def test_one_evening_trip_does_not_drag_the_morning_departure_time():
    """The brief window sits on the learned departure time, so that time has to
    describe a real routine.

    A plain median over every recent scan put "typical" in the afternoon after a
    single evening trip among a week of 8am ones. The window then covered a time
    the user never leaves, every 15-minute tick skipped, and the brief delivered
    nothing at all -- zero in 24 hours on 26 Aug.
    """
    from agent.workflows import _typical_minutes

    morning = [8 * 60 + 2, 8 * 60 + 11, 8 * 60 + 15, 8 * 60 + 24]
    evening = [19 * 60 + 30]

    assert _typical_minutes(sorted(morning + evening)) == 8 * 60 + 15


def test_the_departure_time_still_tracks_a_genuine_shift():
    """Clustering must not freeze the learned time; it only rejects outliers."""
    from agent.workflows import _typical_minutes

    assert _typical_minutes([9 * 60, 9 * 60 + 20, 9 * 60 + 40]) == 9 * 60 + 20
    assert _typical_minutes([]) is None
    assert _typical_minutes([7 * 60]) == 7 * 60


def test_the_brief_window_stays_open_past_the_learned_time():
    """A window closing exactly on a moving median can be stepped over entirely.

    The departure time is recomputed on every scan, so it can advance past a tick
    that had not yet qualified. With the once-a-day claim, missing the window
    once means no brief that day.
    """
    from main import BRIEF_GRACE_MINUTES, BRIEF_LEAD_MINUTES, _brief_window

    opens, closes = _brief_window("08:45")

    assert opens == 8 * 60 + 45 - BRIEF_LEAD_MINUTES
    assert closes == 8 * 60 + 45 + BRIEF_GRACE_MINUTES
    assert opens < 8 * 60 + 45 < closes, "the departure moment itself must be inside"


def test_the_brief_window_is_unset_until_a_departure_is_learned():
    from main import _brief_window

    assert _brief_window(None) is None
    assert _brief_window("not a time") is None


class TestTheBriefCanReachEveryLearnedDepartureTime:
    """The morning brief gates itself twice, and both gates have to agree.

    It only fires inside a window around the departure time a routine has
    actually been observed at — and it only gets the chance to check that window
    when Cloud Scheduler ticks it. Those are two independent constraints, so a
    cron restricted to the morning silently voids every learned time outside it.

    That is not hypothetical. After the demo, the live `work` routine's learned
    departure had drifted to 19:44, giving a window of 19:19-19:59 against a
    production cron of `*/15 5-11 * * *`. The two never intersected, so the brief
    could not fire on any day, and the only trace was a log line reading
    "outside the departure window" from ticks that were never going to match it.
    """

    @staticmethod
    def _production_brief_cron() -> str:
        """The BRIEF_CRON default from deploy.sh's non-demo branch."""
        import re
        from pathlib import Path

        script = Path(__file__).resolve().parents[1] / "deployment" / "deploy.sh"
        defaults = re.findall(r'BRIEF_CRON="\$\{BRIEF_CRON:-([^}]*)\}"', script.read_text())
        assert len(defaults) == 2, f"expected a demo and a production default, got {defaults}"
        return defaults[1]          # demo branch first, production second

    def test_the_production_cron_ticks_at_every_hour(self):
        cron = self._production_brief_cron()
        hour_field = cron.split()[1]
        assert hour_field == "*", (
            f"production BRIEF_CRON is {cron!r}, which only ticks during hours {hour_field}. "
            "A learned departure outside those hours can never be briefed on."
        )

    @pytest.mark.parametrize("departs", ["06:30", "08:45", "11:00", "16:59", "19:44", "23:10"])
    def test_a_window_is_reachable_wherever_the_departure_time_lands(self, departs):
        """Every window the app can compute must contain a tick the cron fires."""
        import main as main_module

        window = main_module._brief_window(departs)
        assert window is not None
        opens, closes = window

        cron = self._production_brief_cron()
        step = int(cron.split()[0].removeprefix("*/"))
        ticks = {h * 60 + m for h in range(24) for m in range(0, 60, step)}

        assert any(opens <= t <= closes for t in ticks), (
            f"a departure at {departs} yields a window of "
            f"{opens // 60:02d}:{opens % 60:02d}-{closes // 60:02d}:{closes % 60:02d}, "
            f"which cron {cron!r} never ticks inside."
        )
