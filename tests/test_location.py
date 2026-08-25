"""Location labels.

Every observation used to be logged at `"here"` — what the camera knows, and
nothing a person can act on. The evening recap rendered it as "Places: here",
which costs a line of a notification and answers nothing.

There is no GPS in this build. What there is, is intent the user has already
stated out loud: "I'm heading to work" is a location transition told to us in
words, and it was being discarded. These tests pin that it is not.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent import watch as watch_module
from agent import workflows as workflows_module
from agent.watch import observe_tick
from agent.workflows import leave_detection
from tests.conftest import SESSION, USER
from tools.vision import DetectedItem, VisionResult
from utils import location as location_module
from utils.location import UNKNOWN, default_label, get_current, normalise, set_current

FRAME = "data:image/jpeg;base64,/9j/stub"


async def _everything(store):
    """Every observation written, regardless of when."""
    return await store.observations_between(
        USER,
        datetime.min.replace(tzinfo=timezone.utc),
        datetime.max.replace(tzinfo=timezone.utc),
    )


@pytest.fixture
def eyes(monkeypatch):
    """The same stub for both call sites, so a scan and a tick agree."""
    state = {"items": ["laptop"]}

    async def fake_scan(_url: str) -> VisionResult:
        return VisionResult(
            items=[DetectedItem(name=n, confidence=0.9, detail="on the desk") for n in state["items"]],
            scene="desk",
        )

    monkeypatch.setattr(watch_module, "scan_frame", fake_scan)
    monkeypatch.setattr(workflows_module, "scan_frame", fake_scan)

    def see(*names):
        state["items"] = list(names)

    return see


class TestTheLabelItself:
    def test_nothing_is_ever_labelled_here(self, cache):
        assert "here" not in default_label().lower()

    async def test_an_unconfigured_agent_admits_it_does_not_know(self, cache, monkeypatch):
        # DEFAULT_LOCATION_LABEL="" is the opt out: say so rather than assume.
        monkeypatch.setattr(location_module, "default_label", lambda: UNKNOWN)
        assert await get_current("no-such-session") == UNKNOWN

    def test_a_spoken_destination_is_cased_for_display(self):
        assert normalise("work") == "Work"
        # Only the first letter. `.title()` would wreck an acronym the user
        # typed correctly.
        assert normalise("NYC office") == "NYC office"
        assert normalise("the gym") == "The gym"
        assert normalise("") == ""

    async def test_the_resting_label_is_the_configured_home_base(self, cache):
        assert await get_current("fresh-session") == default_label()


class TestTransitions:
    async def test_leaving_sets_the_label_and_the_next_tick_inherits_it(
        self, store, cache, eyes
    ):
        eyes("laptop")
        await leave_detection(
            user_id=USER, session_id=SESSION, destination="work", frame_data_url=FRAME
        )
        assert await get_current(SESSION) == "Work"

        # The tick that follows must be tagged with the announced destination,
        # not with whatever the camera can see.
        eyes("phone")
        await observe_tick(user_id=USER, session_id=SESSION, frame_data_url=FRAME)
        written = await _everything(store)
        phone = [o for o in written if o.subject == "phone"]
        assert phone, "the tick wrote nothing"
        assert phone[0].location_label == "Work"
        assert all(o.location_label != "here" for o in written)

    async def test_a_second_announcement_overwrites_the_first(
        self, store, cache, eyes
    ):
        eyes("laptop")
        await leave_detection(
            user_id=USER, session_id=SESSION, destination="work", frame_data_url=FRAME
        )
        assert await get_current(SESSION) == "Work"

        await leave_detection(
            user_id=USER, session_id=SESSION, destination="gym", frame_data_url=FRAME
        )
        assert await get_current(SESSION) == "Gym"

    async def test_the_label_is_per_session(self, cache):
        await set_current("session-a", "work")
        assert await get_current("session-a") == "Work"
        # A different session has heard nothing and must not inherit it.
        assert await get_current("session-b") == default_label()

    async def test_an_explicit_location_still_wins(self, store, cache, eyes):
        eyes("keys")
        await set_current(SESSION, "work")
        await observe_tick(
            user_id=USER, session_id=SESSION, frame_data_url=FRAME, location="Kitchen"
        )
        written = await _everything(store)
        assert [o.location_label for o in written if o.subject == "keys"] == ["Kitchen"]
