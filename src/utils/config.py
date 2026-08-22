"""Environment-driven configuration.

Every external dependency is optional at import time so the service can boot in a
degraded (in-memory) mode for local development and CI.  See `Config.report()`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

load_dotenv()


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    # --- Identity -----------------------------------------------------------
    app_name: str = os.getenv("APP_NAME", "omnistant")
    # Single-user demo default. Real deployments set this per authenticated user.
    default_user_id: str = os.getenv(
        "DEFAULT_USER_ID", "00000000-0000-0000-0000-000000000001"
    )

    # --- Gemini / Vertex ----------------------------------------------------
    # `gemini-3.5-flash`: 1M-token context, natively multimodal, fast enough that
    # a leave-scan feels instant.  Override with GEMINI_MODEL; run
    # `scripts/list_models.py` to see what your credentials actually have.
    model: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    vision_model: str = os.getenv(
        "GEMINI_VISION_MODEL", os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    )
    api_key: str | None = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    use_vertex: bool = _flag("GOOGLE_GENAI_USE_VERTEXAI")
    gcp_project: str | None = os.getenv("GOOGLE_CLOUD_PROJECT")
    gcp_location: str = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")

    # --- Storage ------------------------------------------------------------
    database_url: str | None = os.getenv("DATABASE_URL")
    redis_url: str | None = os.getenv("REDIS_URL")
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", "86400"))
    frame_ttl_seconds: int = int(os.getenv("FRAME_TTL_SECONDS", "900"))

    # Timeline answers are spoken in the user's local time ("you left at 8:47"),
    # so the service needs to know which clock that is.  Storage stays UTC.
    timezone: str = os.getenv("TIMEZONE", "UTC")

    # --- Serving ------------------------------------------------------------
    port: int = int(os.getenv("PORT", "8080"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    # Where the autonomous jobs deliver their results. Without this the agent
    # still runs on schedule, but nobody ever sees what it worked out.
    # Slack: https://api.slack.com/apps -> Incoming Webhooks.
    slack_webhook_url: str | None = os.getenv("SLACK_WEBHOOK_URL")

    # Shared secret required by /api/tasks/* so Cloud Scheduler can call them
    # but the open internet cannot.  Unset => task endpoints are open (dev only).
    task_token: str | None = os.getenv("TASK_TOKEN")

    # --- Behaviour ----------------------------------------------------------
    # An item is "probably still where you left it" for this long before the
    # agent starts hedging in its recall answer.
    recall_stale_after_hours: float = float(os.getenv("RECALL_STALE_AFTER_HOURS", "6"))

    @property
    def genai_available(self) -> bool:
        """True when we can actually reach a Gemini model."""
        return bool(self.api_key) or (self.use_vertex and bool(self.gcp_project))

    @property
    def postgres_enabled(self) -> bool:
        return bool(self.database_url)

    @property
    def redis_enabled(self) -> bool:
        return bool(self.redis_url)

    def report(self) -> dict[str, object]:
        """Human-readable summary of which subsystems are live vs. degraded."""
        return {
            "model": self.model,
            "gemini": "live" if self.genai_available else "stub (no credentials)",
            "gemini_backend": "vertex-ai" if self.use_vertex else "google-ai-studio",
            "postgres": "live" if self.postgres_enabled else "in-memory fallback",
            "notifications": "slack" if self.slack_webhook_url else "none",
            "redis": "live" if self.redis_enabled else "in-process fallback",
        }


def tz() -> ZoneInfo:
    """The user's local timezone, falling back to UTC if TIMEZONE is bogus."""
    try:
        return ZoneInfo(get_config().timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
