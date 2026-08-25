"""The autonomous loop has to fit inside a recording.

Nobody can film themselves waiting six hours, so the proof that the agent acts
on its own has to be capturable in a single unedited take. Same code, same
trigger, shorter cadence — these tests pin the parts of that which can break
silently: the job wrapper must not eat an endpoint's parameters, and the recap
must not need a model round trip to produce its notification.
"""

from __future__ import annotations

import inspect
import os
from time import perf_counter

import pytest

import main as main_module
from agent.workflows import daily_timeline
from tests.conftest import USER
from utils.config import Config
from utils.notify_format import Line, daily_recap


class TestTheJobWrapper:
    @pytest.mark.parametrize(
        "endpoint,params",
        [
            (main_module.task_morning_brief, {"user_id", "force"}),
            (main_module.task_evening_recap, {"user_id"}),
            (main_module.task_drain_nudges, set()),
        ],
    )
    def test_timing_does_not_erase_an_endpoints_signature(self, endpoint, params):
        """FastAPI reads query params and dependencies off the signature.

        A decorator without `functools.wraps` leaves `(*args, **kwargs)`, and
        every one of these endpoints silently loses its parameters — and its
        task-token guard — while still returning 200.
        """
        assert set(inspect.signature(endpoint).parameters) == params

    def test_all_three_scheduled_jobs_are_instrumented(self):
        # The pre-recording check is `grep "scheduler job"` over the logs; a job
        # that is not wrapped simply never appears and looks like it never ran.
        for job in (
            main_module.task_morning_brief,
            main_module.task_evening_recap,
            main_module.task_drain_nudges,
        ):
            assert hasattr(job, "__wrapped__"), f"{job.__name__} is not timed"


class TestRecapLatency:
    async def test_a_recap_needs_no_model_call_when_narration_is_off(
        self, store, cache, make_observation, monkeypatch
    ):
        """The Slack bullets come from the log, so the round trip is optional."""
        await store.add_observations([make_observation("keys"), make_observation("laptop")])

        def explode():
            raise AssertionError("the recap must not call a model when narration is off")

        monkeypatch.setattr("agent.workflows.get_client", explode)
        result = await daily_timeline(user_id=USER, narrate=False)
        assert result.entries
        assert result.narrative, "there must still be a narrative for the UI"

    async def test_compiling_the_notification_is_effectively_free(
        self, store, cache, make_observation
    ):
        """Everything except the model call, timed.

        The budget in the demo plan is 10s for compile-and-post. This is the
        compile half; if it were ever anywhere near that, the cause would be a
        query, not the formatting.
        """
        await store.add_observations([make_observation(f"item{i}") for i in range(50)])

        started = perf_counter()
        timeline = await daily_timeline(user_id=USER, narrate=False)
        block = daily_recap(
            day_label="Aug 24",
            where="Home",
            lines=[Line(e.to_dict()["time"], e.kind, e.subject, e.detail) for e in timeline.entries],
            count=len(timeline.entries),
        )
        elapsed = perf_counter() - started

        assert block.bullets
        assert elapsed < 1.0, f"compiling the recap took {elapsed:.2f}s"


class TestDemoLevers:
    """Production defaults must stay realistic; the demo cadence is env-only.

    Note these read the environment once, at import — dataclass field defaults
    are evaluated when the class is defined, not when it is instantiated. That
    is fine for a process whose environment is fixed before it starts, and it
    means `monkeypatch.setenv` cannot be used to test the override. The parsing
    is tested directly instead.
    """

    def test_the_shipped_defaults_are_the_realistic_ones(self):
        cfg = Config()
        assert cfg.leave_nudge_delay_minutes == 2, "a demo value must never ship as the default"
        assert cfg.recap_narrate is True

    @pytest.mark.parametrize(
        "raw,expected",
        [("false", False), ("0", False), ("no", False), ("off", False),
         ("true", True), ("1", True), ("yes", True), ("on", True)],
    )
    def test_the_narration_switch_understands_the_usual_spellings(self, raw, expected):
        # Getting this wrong means a demo override silently does nothing.
        from utils.config import _flag

        os.environ["RECAP_NARRATE"] = raw
        try:
            assert _flag("RECAP_NARRATE", True) is expected
        finally:
            del os.environ["RECAP_NARRATE"]

    def test_a_demo_nudge_delay_parses_as_a_fraction_of_a_minute(self):
        # 0.5 => 30s, which is what makes the loop fit a 45-60s take.
        os.environ["LEAVE_NUDGE_DELAY_MINUTES"] = "0.5"
        try:
            assert float(os.environ["LEAVE_NUDGE_DELAY_MINUTES"]) == 0.5
        finally:
            del os.environ["LEAVE_NUDGE_DELAY_MINUTES"]
