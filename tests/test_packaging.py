"""The deployed image must contain everything the app imports.

`requirements.txt` is what the Dockerfile installs; `pyproject.toml` is what
local development uses. They drift the moment someone runs `uv add` without
re-exporting — and the failure is silent, because every optional subsystem here
degrades gracefully rather than crashing.

That is exactly what happened once: sqlalchemy and greenlet were added for
Postgres-backed sessions, requirements.txt was not regenerated, and the deployed
service quietly fell back to in-memory sessions. Everything looked healthy.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
PYPROJECT = ROOT / "pyproject.toml"


def _normalise(name: str) -> str:
    """PEP 503 normalisation: Foo_Bar and foo-bar are the same package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared_dependencies() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text())
    names = set()
    for spec in data["project"]["dependencies"]:
        # "psycopg[binary,pool]>=3.3.4" -> "psycopg"
        names.add(_normalise(re.split(r"[\[<>=!;\s]", spec, maxsplit=1)[0]))
    return names


def _pinned_in_requirements() -> set[str]:
    names = set()
    for line in REQUIREMENTS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.add(_normalise(re.split(r"[\[<>=!;\s]", line, maxsplit=1)[0]))
    return names


def test_requirements_covers_every_declared_dependency():
    missing = _declared_dependencies() - _pinned_in_requirements()
    assert not missing, (
        f"requirements.txt is missing {sorted(missing)}. The Docker image would "
        f"not have them, and the service degrades silently rather than failing. "
        f"Regenerate with:\n"
        f"  uv export --no-hashes --no-dev --format requirements-txt "
        f"| grep -v '^-e \\.' > requirements.txt"
    )


@pytest.mark.parametrize(
    "package",
    [
        "google-adk",     # the agent runtime
        "fastapi",        # the service
        "psycopg",        # the event log
        "redis",          # session cache
        "sqlalchemy",     # ADK DatabaseSessionService
        "greenlet",       # SQLAlchemy's async bridge; missing = silent fallback
        "httpx",          # Slack delivery
    ],
)
def test_runtime_critical_packages_are_pinned(package):
    """Each of these, if absent, degrades something quietly instead of crashing."""
    assert _normalise(package) in _pinned_in_requirements()
