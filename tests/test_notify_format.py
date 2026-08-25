"""Notification wording.

The daily recap arrived as a paragraph: correct in every fact, and the wrong
shape for the person it is built for. These tests pin the house style, because
formatting rules that live only in a style guide drift back to prose one
convenient exception at a time.
"""

from __future__ import annotations

from utils.notify_format import (
    MAX_NAMES_PER_BULLET,
    Line,
    bullets_from_lines,
    daily_recap,
    item_status,
    leaving_without,
)

# Verbatim from the 21:00 Slack message on 24 Aug 2026 that prompted the change.
REAL_DAY = [
    Line("11:09 AM", "item", "airpod"),
    Line("11:09 AM", "item", "airpods case"),
    Line("11:09 AM", "item", "charging cable"),
    Line("11:09 AM", "item", "phone"),
    Line("11:09 AM", "item", "laptop"),
    Line("11:10 AM", "activity", "charging cable left view"),
    Line("11:10 AM", "activity", "phone left view"),
]


class TestBullets:
    def test_one_moment_is_one_bullet_not_five(self):
        # Five items seen in the same second is one thing that happened.
        bullets = bullets_from_lines(REAL_DAY)
        assert len(bullets) == 2

    def test_every_bullet_leads_with_its_time(self):
        for bullet in bullets_from_lines(REAL_DAY):
            assert bullet.startswith("11:0") or bullet.startswith("11:1"), bullet

    def test_no_bullet_runs_past_scannable_length(self):
        # Rule 4. The paragraph this replaced ran to 47 words in one sentence.
        for bullet in bullets_from_lines(REAL_DAY):
            phrase = bullet.split("—", 1)[1]
            assert len(phrase.split()) <= 8, bullet

    def test_a_long_list_is_truncated_with_a_count_not_wrapped(self):
        many = [Line("9:00 AM", "item", n) for n in ("a", "b", "c", "d", "e")]
        assert bullets_from_lines(many) == [
            f"9:00 AM — Saw a, b, c +{5 - MAX_NAMES_PER_BULLET} more"
        ]

    def test_seen_and_gone_never_share_a_bullet(self):
        # Rule 5: no compound sentences. "X, while Y left your view" was the
        # original output and is exactly what this forbids.
        lines = [Line("9:00 AM", "item", "keys"), Line("9:00 AM", "activity", "phone left view")]
        bullets = bullets_from_lines(lines)
        assert bullets == ["9:00 AM — Saw keys", "9:00 AM — Phone out of view"]

    def test_repeat_sightings_of_one_item_are_said_once(self):
        lines = [Line("9:00 AM", "item", "keys"), Line("9:00 AM", "item", "keys")]
        assert bullets_from_lines(lines) == ["9:00 AM — Saw keys"]


class TestDailyRecap:
    def test_it_renders_the_house_style(self):
        text = daily_recap(day_label="Aug 24", where="Home", lines=REAL_DAY, count=8).to_mrkdwn()
        assert text.startswith("🧠 *Your Day* — Aug 24")
        assert "📍 *Where:* Home" in text
        assert "*Timeline*" in text
        assert "• 11:09 AM" in text
        assert text.rstrip().endswith("_8 things noticed today_")

    def test_a_thin_day_says_so_briefly_and_without_apologising(self):
        block = daily_recap(day_label="Aug 24", where="Home", lines=REAL_DAY[:1], count=2)
        assert block.footer == "Only a few things noticed today"
        # The original opened "extremely sparse, covering only a single minute",
        # which reads like an error message about the user's day.
        assert "sparse" not in block.to_mrkdwn().lower()

    def test_where_is_never_the_word_here(self):
        text = daily_recap(day_label="Aug 24", where="Work", lines=REAL_DAY, count=8).to_mrkdwn()
        assert ":* here" not in text

    def test_plain_text_fallback_carries_the_same_facts(self):
        plain = daily_recap(day_label="Aug 24", where="Home", lines=REAL_DAY, count=8).to_plain()
        assert "Your Day — Aug 24" in plain
        assert "Where: Home" in plain
        # No mrkdwn syntax leaking into the fallback clients read.
        assert "*" not in plain and "•" not in plain


class TestTheOtherTwoNotifications:
    def test_leaving_alert_leads_with_the_door_and_the_destination(self):
        text = leaving_without(
            destination="work", missing=["keys", "water bottle"],
            last_seen=["keys — Home, 8:42 AM"], when="about 2 minutes ago",
        ).to_mrkdwn()
        assert text.startswith("🚪 *You left without something*")
        assert "📍 *Where:* Work" in text
        assert "• Missing: keys, water bottle" in text

    def test_recall_status_names_what_it_cannot_vouch_for(self):
        text = item_status(
            destination="work", unverified=["keys"], expected=["phone", "keys"],
        ).to_mrkdwn()
        assert text.startswith("🔍 *Before you leave*")
        assert "• Can't vouch for: keys" in text
        assert text.rstrip().endswith("_Usually taken: phone, keys_")

    def test_an_all_clear_reads_as_an_all_clear(self):
        block = item_status(destination="work", unverified=[], expected=["phone"])
        assert block.emoji == "✅"
        assert "good to go" in block.header.lower()

    def test_all_three_share_the_same_skeleton(self):
        # The point of the shared formatter: these cannot drift apart.
        blocks = [
            daily_recap(day_label="Aug 24", where="Home", lines=REAL_DAY, count=8),
            leaving_without(destination="work", missing=["keys"], last_seen=[], when="just now"),
            item_status(destination="work", unverified=["keys"], expected=["keys"]),
        ]
        for block in blocks:
            text = block.to_mrkdwn()
            assert text[0] not in "*_•", "every notification opens with an emoji anchor"
            assert "📍 *Where:*" in text
            assert text.count("\n\n") >= 2, "sections must be visually separated"
