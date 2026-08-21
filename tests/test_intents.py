"""Intent recognition — the deterministic first pass.

The rules layer is what carries a live demo: it answers without a model call, so
it cannot be rate-limited and cannot wobble. These cases are the phrasings the
demo actually uses.
"""

from __future__ import annotations

import pytest

from agent.intents import Intent, classify_rules


@pytest.mark.parametrize(
    "utterance,destination",
    [
        ("I'm going to work", "work"),
        ("heading out to the gym", "gym"),
        ("off to the office", "office"),
        ("I'm about to leave for work", "work"),
        ("on my way to the shops", "shops"),
        ("I'm leaving", None),
        ("heading out", None),
    ],
)
def test_recognises_leaving(utterance, destination):
    result = classify_rules(utterance)
    assert result.intent is Intent.LEAVE_DETECTION
    assert result.destination == destination
    assert result.needs_camera is True


@pytest.mark.parametrize(
    "utterance,item",
    [
        ("Where are my AirPods?", "airpods"),
        ("where's my wallet", "wallet"),
        ("where did i put my keys", "keys"),
        ("have you seen my glasses", "glasses"),
        ("do you know where my badge is", "badge"),
        ("where is my laptop", "laptop"),
        ("where are my keys?", "keys"),
    ],
)
def test_recognises_item_questions(utterance, item):
    result = classify_rules(utterance)
    assert result.intent is Intent.ITEM_RECALL
    assert result.item == item


@pytest.mark.parametrize(
    "utterance,time_reference",
    [
        ("What did I do today?", None),
        ("recap my day", None),
        ("what was I doing at 2pm", "2pm"),
        ("what was I doing at noon", "noon"),
    ],
)
def test_recognises_timeline_questions(utterance, time_reference):
    result = classify_rules(utterance)
    assert result.intent is Intent.DAILY_TIMELINE
    assert result.time_reference == time_reference
    assert result.needs_camera is False


def test_recognises_a_stated_placement():
    result = classify_rules("I put my keys on the hall table")
    assert result.intent is Intent.LOG_OBSERVATION
    assert result.item == "keys"
    assert result.slots["place"] == "hall table"


def test_recognises_a_stated_location():
    result = classify_rules("I'm at the office")
    assert result.intent is Intent.LOG_OBSERVATION
    assert result.slots == {"place": "office", "kind": "location"}


@pytest.mark.parametrize("utterance", ["what's the weather", "hello", "", "   "])
def test_declines_to_guess_on_unrelated_input(utterance):
    """An unknown result is what escalates to the model. A confident wrong guess
    would skip that escalation entirely."""
    result = classify_rules(utterance)
    assert result.intent is Intent.UNKNOWN
    assert result.confidence == 0.0
