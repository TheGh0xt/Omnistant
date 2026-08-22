"""Prove that a conversation survives the process that started it.

Needs a real DATABASE_URL. Writes a session with two events, drops the service,
builds a fresh one — which is what a restart or a second Cloud Run instance
looks like — and reads the conversation back.

    uv run python scripts/verify_session_durability.py
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from google.adk.events import Event  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from agent.engine import build_session_service  # noqa: E402
from utils.config import get_config  # noqa: E402
from utils.logger import configure_logging  # noqa: E402

APP = "omnistant"
USER = "u-durability-check"
SECRET = "my backpack is bright orange"


def _say(role: str, text: str) -> Event:
    return Event(
        author="user" if role == "user" else "personal_context_agent",
        content=types.Content(role=role, parts=[types.Part.from_text(text=text)]),
    )


async def main() -> int:
    configure_logging("ERROR")
    cfg = get_config()
    if not cfg.postgres_enabled:
        print("DATABASE_URL is not set — nothing to verify. Run `docker compose up -d` first.")
        return 1

    session_id = f"durability-{uuid.uuid4().hex[:8]}"
    service, backend = build_session_service(cfg)
    print(f"session backend: {backend}")
    if backend != "postgres":
        print("Expected a postgres-backed session service; got the fallback.")
        return 1

    await service.create_session(app_name=APP, user_id=USER, session_id=session_id, state={})
    for role, text in (("user", SECRET), ("model", "Noted — bright orange backpack.")):
        current = await service.get_session(app_name=APP, user_id=USER, session_id=session_id)
        await service.append_event(current, _say(role, text))
    print(f"wrote a 2-turn conversation to session {session_id}")

    # Everything the process was holding goes away here.
    del service

    fresh, _ = build_session_service(cfg)
    recovered = await fresh.get_session(app_name=APP, user_id=USER, session_id=session_id)
    if recovered is None:
        print("FAIL: the session did not survive")
        return 1

    spoken = [p.text for e in recovered.events for p in (e.content.parts or []) if p.text]
    print(f"recovered {len(recovered.events)} events from a fresh service instance:")
    for line in spoken:
        print(f"  · {line}")

    if not any(SECRET in line for line in spoken):
        print("FAIL: the conversation content was lost")
        return 1

    gone = await InMemorySessionService().get_session(
        app_name=APP, user_id=USER, session_id=session_id
    )
    print(f"\nsame lookup against InMemorySessionService: {gone!r}")
    print("PASS: conversation history survives a fresh process.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
