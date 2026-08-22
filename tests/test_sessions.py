"""Session durability.

The agent is only coherent across turns if the session outlives the process
handling them. With ADK's InMemorySessionService it does not: a restart, or a
second Cloud Run instance, loses the conversation mid-sentence.

These tests pin the selection logic. The end-to-end proof that a Postgres-backed
session survives a fresh process needs a real database and lives in
`scripts/verify_session_durability.py`.
"""

from __future__ import annotations

import pytest
from google.adk.sessions import InMemorySessionService

from agent.engine import _sqlalchemy_url, build_session_service
from utils.config import get_config


@pytest.mark.parametrize(
    "dsn,expected",
    [
        ("postgresql://u:p@h:5433/db", "postgresql+psycopg://u:p@h:5433/db"),
        ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        # Already-qualified URLs must not be mangled.
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        # Cloud SQL unix-socket form, which has no host before the '/'.
        (
            "postgresql://u:p@/omnistant?host=/cloudsql/proj:reg:inst",
            "postgresql+psycopg://u:p@/omnistant?host=/cloudsql/proj:reg:inst",
        ),
    ],
)
def test_dsn_is_rewritten_for_sqlalchemy(dsn, expected):
    """Bare postgresql:// resolves to psycopg2 — not installed, and not async."""
    assert _sqlalchemy_url(dsn) == expected


def test_falls_back_to_in_memory_without_a_database():
    """No DATABASE_URL is a valid (degraded) configuration, not a crash."""
    cfg = get_config()
    assert not cfg.postgres_enabled, "the test suite must run without a database"

    service, backend = build_session_service(cfg)

    assert isinstance(service, InMemorySessionService)
    assert backend == "in-memory"


def test_a_broken_database_url_does_not_stop_the_service_booting(monkeypatch):
    """A session backend that cannot connect must degrade, not take the app down."""
    cfg = get_config()
    monkeypatch.setattr(type(cfg), "postgres_enabled", property(lambda self: True))
    monkeypatch.setattr(type(cfg), "database_url", property(lambda self: "not-a-valid-dsn"))

    service, backend = build_session_service(cfg)

    assert isinstance(service, InMemorySessionService)
    assert "fallback" in backend
