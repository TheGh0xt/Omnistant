"""Outbound notifications.

The autonomous jobs are only meaningful if their output reaches a person, so the
delivery path gets the same treatment as the workflows: real payload shapes, and
failures that degrade instead of taking the job down.
"""

from __future__ import annotations

import json

import httpx
import pytest

from utils.notify import (
    MAX_TEXT,
    Notification,
    NullNotifier,
    SlackNotifier,
    build_notifier,
)

WEBHOOK = "https://hooks.slack.com/services/T000/B000/xxxx"


def slack_transport(captured: list[httpx.Request], *, status: int = 200, body: str = "ok"):
    """A stand-in for Slack that records what we actually sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_code=status, text=body)

    return httpx.MockTransport(handler)


@pytest.fixture
def sent(monkeypatch):
    captured: list[httpx.Request] = []
    original = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = slack_transport(captured, **patched.response)
        return original(*args, **kwargs)

    patched.response = {}
    monkeypatch.setattr(httpx, "AsyncClient", patched)
    return captured, patched


class TestSlackPayload:
    async def test_posts_to_the_webhook_url(self, sent):
        captured, _ = sent
        assert await SlackNotifier(WEBHOOK).send(Notification(title="T", body="B")) is True
        assert str(captured[0].url) == WEBHOOK

    async def test_always_includes_fallback_text(self, sent):
        """Slack uses `text` for the push preview; blocks alone show a blank alert."""
        captured, _ = sent
        await SlackNotifier(WEBHOOK).send(
            Notification(title="Before you leave", body="You're missing your AirPods.")
        )
        payload = json.loads(captured[0].content)
        assert "Before you leave" in payload["text"]
        assert "AirPods" in payload["text"]

    async def test_renders_facts_as_fields(self, sent):
        captured, _ = sent
        await SlackNotifier(WEBHOOK).send(
            Notification(title="T", body="B", facts=[("Missing", "airpods, badge")])
        )
        blocks = json.loads(captured[0].content)["blocks"]
        fields = [b for b in blocks if b.get("type") == "section" and "fields" in b]
        assert fields and "airpods, badge" in fields[0]["fields"][0]["text"]

    async def test_urgent_is_visually_marked(self, sent):
        captured, _ = sent
        await SlackNotifier(WEBHOOK).send(Notification(title="Heads up", body="B", urgent=True))
        header = json.loads(captured[0].content)["blocks"][0]
        assert header["text"]["text"].startswith("⚠️")

    async def test_slack_caps_fields_at_ten(self, sent):
        """Slack rejects a section with more than 10 fields outright."""
        captured, _ = sent
        many = [(f"k{i}", f"v{i}") for i in range(25)]
        await SlackNotifier(WEBHOOK).send(Notification(title="T", body="B", facts=many))
        blocks = json.loads(captured[0].content)["blocks"]
        fields = [b for b in blocks if b.get("type") == "section" and "fields" in b][0]["fields"]
        assert len(fields) == 10

    async def test_long_bodies_are_truncated(self, sent):
        captured, _ = sent
        await SlackNotifier(WEBHOOK).send(Notification(title="T", body="x" * 10_000))
        payload = json.loads(captured[0].content)
        assert len(payload["text"]) <= MAX_TEXT
        assert len(payload["blocks"][1]["text"]["text"]) <= MAX_TEXT


class TestFailuresDegrade:
    async def test_a_dead_webhook_does_not_raise(self, sent):
        _, patched = sent
        patched.response = {"status": 404, "body": "no_service"}
        assert await SlackNotifier(WEBHOOK).send(Notification(title="T", body="B")) is False

    async def test_slacks_200_plus_error_body_counts_as_failure(self, sent):
        """A revoked webhook answers 200 with an error string, not JSON."""
        _, patched = sent
        patched.response = {"status": 200, "body": "invalid_token"}
        assert await SlackNotifier(WEBHOOK).send(Notification(title="T", body="B")) is False

    async def test_connection_errors_return_false(self, monkeypatch):
        """A notification is best-effort: the scheduled job must still finish."""
        original = httpx.AsyncClient

        def patched(*args, **kwargs):
            def handler(request):
                raise httpx.ConnectError("no route to host", request=request)

            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)
        assert await SlackNotifier(WEBHOOK).send(Notification(title="T", body="B")) is False

    async def test_timeouts_return_false_rather_than_propagating(self, monkeypatch):
        original = httpx.AsyncClient

        def patched(*args, **kwargs):
            def handler(request):
                raise httpx.ReadTimeout("slow", request=request)

            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", patched)
        assert await SlackNotifier(WEBHOOK).send(Notification(title="T", body="B")) is False


class TestSelection:
    async def test_no_webhook_configured_is_not_an_error(self):
        """The agent must still run its jobs with no channel wired up."""
        notifier = build_notifier()
        assert isinstance(notifier, NullNotifier)
        assert notifier.enabled is False
        assert await notifier.send(Notification(title="T", body="B")) is False

    def test_plain_text_includes_the_facts(self):
        note = Notification(title="Title", body="Body", facts=[("Missing", "keys")])
        text = note.as_plain_text()
        assert "Title" in text and "Body" in text and "Missing: keys" in text
