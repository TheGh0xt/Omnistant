"""Test fixtures.

The suite runs entirely against the in-memory store and cache, with the Gemini
calls stubbed. That keeps it hermetic — no Postgres, no Redis, no API key, no
quota — so it can run in CI and on a laptop with the network off.

The integration test that does exercise real Postgres is opt-in via
DATABASE_URL and skips itself when that is unset.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

# Force the hermetic path before any project module reads config.
#
# These are set to "" rather than deleted on purpose: `utils.config` calls
# `load_dotenv()` at import, and python-dotenv will happily fill in a key that is
# absent — but it will not override one that is already present. An empty string
# is present-but-falsey, which is exactly what we want: no Postgres, no Redis, no
# API key, so `Config.genai_available` is False and every Gemini call takes the
# deterministic stub path.
for _var in ("DATABASE_URL", "REDIS_URL", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
    os.environ[_var] = ""
os.environ["TIMEZONE"] = "UTC"

from utils.config import get_config  # noqa: E402

assert not get_config().genai_available, "tests must not reach a live model"
assert not get_config().postgres_enabled, "tests must not reach a live database"

from utils import cache as cache_module  # noqa: E402
from utils import db as db_module  # noqa: E402
from utils.cache import Cache, MemoryBackend  # noqa: E402
from utils.db import MemoryStore, Observation, Routine  # noqa: E402

USER = "00000000-0000-0000-0000-000000000001"
SESSION = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def store(monkeypatch) -> MemoryStore:
    instance = MemoryStore()
    monkeypatch.setattr(db_module, "_store", instance)
    return instance


@pytest.fixture
def cache(monkeypatch) -> Cache:
    instance = Cache(MemoryBackend())
    monkeypatch.setattr(cache_module, "_cache", instance)
    return instance


@pytest.fixture
def now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def make_observation(now):
    def _make(subject: str, *, minutes_ago: float = 0, location: str = "home",
              kind: str = "item", method: str = "visual", confidence: float = 0.9,
              detail: str = "", content: dict | None = None) -> Observation:
        return Observation(
            user_id=USER, observation_type=kind, subject=subject,
            content=content if content is not None else {"detail": detail},
            observed_at=now - timedelta(minutes=minutes_ago),
            location_label=location, confidence=confidence,
            verification_method=method, session_id=SESSION,
        )
    return _make


@pytest.fixture
def work_routine() -> Routine:
    return Routine(
        user_id=USER, routine_name="work",
        expected_items=["phone", "wallet", "keys", "laptop", "airpods"],
        location_label="office", times_observed=4,
    )
