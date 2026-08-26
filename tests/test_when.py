"""Timestamps have to say which day.

Read at 6:28 on Wednesday, "last seen at 11:09 AM" says the agent saw the thing
five hours ago. It had actually seen it the previous morning. The recall answer
was correct about the clock and wrong about the only thing being asked, which is
whether the sighting is worth acting on.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent.workflows import _fmt_time, _fmt_when, _when_phrase
from utils.config import tz


def ago(**kw) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kw)


class TestTheDisplayForm:
    def test_today_is_the_clock_alone(self):
        assert _fmt_when(ago(hours=2)) == _fmt_time(ago(hours=2))

    def test_yesterday_says_yesterday(self):
        assert _fmt_when(ago(days=1, hours=2)).startswith("yesterday, ")

    def test_earlier_this_week_names_the_day(self):
        when = _fmt_when(ago(days=3, hours=2))
        assert when.split(",")[0] in {
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"
        }

    def test_beyond_a_week_gives_a_date(self):
        # A weekday name stops being useful once it could mean two dates.
        assert _fmt_when(ago(days=12)).split(",")[0][0].isdigit()

    def test_the_clock_survives_in_every_form(self):
        for days in (0, 1, 3, 12):
            assert ":" in _fmt_when(ago(days=days, hours=2))

    def test_no_form_leads_with_a_preposition(self):
        # This string goes into a details table where "at" would be wrong.
        for days in (0, 1, 3, 12):
            assert not _fmt_when(ago(days=days, hours=2)).startswith(("at ", "on "))


class TestTheSpokenForm:
    @pytest.mark.parametrize(
        "display,expected",
        [
            ("11:09 AM", "at 11:09 AM"),
            ("yesterday, 11:09 AM", "yesterday, 11:09 AM"),
            ("Sunday, 11:09 AM", "Sunday, 11:09 AM"),
            ("14 Aug, 11:09 AM", "on 14 Aug, 11:09 AM"),
        ],
    )
    def test_it_reads_as_a_sentence(self, display, expected):
        assert _when_phrase(display) == expected

    def test_no_answer_ever_says_at_yesterday(self):
        for days in (0, 1, 3, 12):
            sentence = f"Last confirmed here {_when_phrase(_fmt_when(ago(days=days, hours=2)))}"
            assert "at yesterday" not in sentence
            assert "at Sunday" not in sentence
            assert sentence.count(" at at ") == 0


class TestItReachesTheAnswer:
    async def test_a_day_old_sighting_is_dated_in_the_recall_answer(
        self, store, cache, make_observation
    ):
        from agent.workflows import item_recall

        await store.add_observations([
            make_observation("keys", minutes_ago=60 * 26, location="Home", detail="on the hall table"),
        ])
        result = await item_recall(user_id=make_observation("x").user_id, item="keys")
        assert result.found
        assert "yesterday" in result.sightings[0].time_str, result.sightings[0].time_str
        assert "yesterday" in result.speech, result.speech
