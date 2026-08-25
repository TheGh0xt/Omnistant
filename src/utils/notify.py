"""Outbound notifications.

The autonomous jobs are the whole "agent" claim: they run with nobody watching.
But a result nobody sees is not an action taken — before this module existed,
`/api/tasks/morning-brief` computed a genuinely useful warning and returned it as
JSON to Cloud Scheduler, where it went nowhere.

One small interface with a Slack implementation. Adding Telegram, email or web
push means writing one `Notifier` subclass and one line in `build_notifier` —
nothing in the workflows knows which channel is in use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import get_config
from .logger import get_logger
from .notify_format import Block

log = get_logger(__name__)

# Slack rejects payloads over 4KB of blocks; a recap is prose, so cap it well
# under that and let the UI carry the full version.
MAX_TEXT = 2800
TIMEOUT_SECONDS = 8.0


@dataclass
class Notification:
    """One thing worth telling a person about, independent of channel."""

    title: str
    body: str
    # Short "label: value" pairs shown under the body, e.g. missing items.
    facts: list[tuple[str, str]] = field(default_factory=list)
    # Something went wrong / needs attention, as opposed to an all-clear.
    urgent: bool = False
    # Preferred over title/body/facts when present: a pre-formatted block in the
    # house style (see notify_format). The older fields stay because `send` must
    # keep working for any caller that has not been moved across, and because
    # `text` still has to carry a plain fallback either way.
    block: Block | None = None

    def as_plain_text(self) -> str:
        if self.block is not None:
            return self.block.to_plain()[:MAX_TEXT]
        lines = [self.title, "", self.body]
        if self.facts:
            lines.append("")
            lines.extend(f"{label}: {value}" for label, value in self.facts)
        return "\n".join(lines)[:MAX_TEXT]


class Notifier:
    """Base class. `send` must never raise — a failed notification must not fail the job."""

    name = "none"
    enabled = False

    async def send(self, note: Notification) -> bool:
        raise NotImplementedError


class NullNotifier(Notifier):
    """No channel configured. Logs, so the output is at least observable."""

    name = "log-only"
    enabled = False

    async def send(self, note: Notification) -> bool:
        log.info(
            "notification (no channel configured)",
            extra={"title": note.title, "body": note.body, "facts": dict(note.facts)},
        )
        return False


class SlackNotifier(Notifier):
    """Slack incoming webhook.

    Create one at https://api.slack.com/apps → your app → Incoming Webhooks →
    "Add New Webhook to Workspace". The resulting URL is the only credential;
    posting to it needs no token and no headers.
    """

    name = "slack"
    enabled = True

    def __init__(self, webhook_url: str) -> None:
        self._url = webhook_url

    def _blocks(self, note: Notification) -> list[dict[str, Any]]:
        if note.block is not None:
            # One mrkdwn section, not a header block: Slack's `header` type is
            # plain_text only, so it cannot carry the emoji-plus-bold anchor the
            # house style opens with, and it forces a second block just to say
            # where you are.
            return [
                {"type": "section", "text": {"type": "mrkdwn", "text": note.block.to_mrkdwn()[:MAX_TEXT]}},
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": "Omnistant · sent automatically"}],
                },
            ]
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{'⚠️ ' if note.urgent else ''}{note.title}"[:150]},
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": note.body[:MAX_TEXT]}},
        ]
        if note.facts:
            blocks.append(
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*{label}*\n{value}"[:2000]}
                        for label, value in note.facts[:10]  # Slack caps fields at 10
                    ],
                }
            )
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Omnistant · sent automatically"}],
            }
        )
        return blocks

    async def send(self, note: Notification) -> bool:
        payload = {
            # `text` is the notification preview and the fallback for clients
            # that cannot render blocks. Slack warns if it is missing.
            "text": note.as_plain_text(),
            "blocks": self._blocks(note),
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                response = await client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            log.warning("slack notification failed", extra={"error": str(exc)})
            return False

        if response.status_code != 200 or response.text.strip() != "ok":
            # Slack answers a bad webhook with 200 + an error string, or 4xx.
            log.warning(
                "slack rejected the notification",
                extra={"status": response.status_code, "response": response.text[:200]},
            )
            return False

        log.info("slack notification sent", extra={"title": note.title})
        return True


_notifier: Notifier | None = None


def build_notifier() -> Notifier:
    cfg = get_config()
    if cfg.slack_webhook_url:
        log.info("notifications: slack")
        return SlackNotifier(cfg.slack_webhook_url)
    log.info("notifications: none configured (set SLACK_WEBHOOK_URL)")
    return NullNotifier()


def init_notifier() -> Notifier:
    global _notifier
    _notifier = build_notifier()
    return _notifier


def get_notifier() -> Notifier:
    if _notifier is None:
        return NullNotifier()
    return _notifier
