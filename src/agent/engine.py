"""ADK agent runtime.

Wires the Gemini model, the tool set and session state into a single
`AgentEngine` the API layer can call once per user turn.

Two paths deliberately coexist:

  * `handle()` — the conversational path. The LLM agent decides which tool to
    call. This is what the user talks to.
  * the workflow functions in `agent.workflows` — called directly by the
    scheduled/autonomous endpoints, where there is no conversation and we want
    the decision to be deterministic.

Both read and write the same observation log, so the agent's memory is identical
whether a human or a cron trigger caused it to act.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.genai import types

from agent.intents import Intent, IntentResult, classify
from agent.prompts import SYSTEM_INSTRUCTION
from tools.registry import AGENT_TOOLS
from utils.cache import get_cache
from utils.config import get_config
from utils.errors import ModelQuotaError, is_quota_error, retry_after_seconds
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class AgentReply:
    text: str
    intent: str = "unknown"
    intent_confidence: float = 0.0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    workflow_results: list[dict[str, Any]] = field(default_factory=list)
    session_id: str = ""
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.text,
            "intent": self.intent,
            "intent_confidence": round(self.intent_confidence, 2),
            "tool_calls": self.tool_calls,
            "workflow_results": self.workflow_results,
            "session_id": self.session_id,
            "degraded": self.degraded,
        }


def _sqlalchemy_url(dsn: str) -> str:
    """Rewrite a libpq DSN into the SQLAlchemy async form.

    ADK's DatabaseSessionService wants an async engine. Bare `postgresql://`
    resolves to psycopg2, which is not installed and is not async; psycopg 3 is,
    and its dialect is `postgresql+psycopg`.
    """
    return re.sub(r"^postgres(?:ql)?(?:\+\w+)?://", "postgresql+psycopg://", dsn)


def build_session_service(cfg) -> tuple[BaseSessionService, str]:
    """Durable sessions when there is a database, in-memory when there is not.

    This is what lets a conversation survive a restart, and what lets more than
    one Cloud Run instance serve the same user: with the in-memory service, turn
    two routed to a different container finds no session and the agent forgets
    the conversation mid-sentence.
    """
    if not cfg.postgres_enabled:
        log.warning("no DATABASE_URL — conversations will be lost on restart")
        return InMemorySessionService(), "in-memory"
    try:
        from google.adk.sessions import DatabaseSessionService

        service = DatabaseSessionService(db_url=_sqlalchemy_url(cfg.database_url))
        log.info("sessions are durable (postgres-backed)")
        return service, "postgres"
    except Exception as exc:  # noqa: BLE001 - never let this stop the service booting
        log.warning(
            "durable sessions unavailable, falling back to in-memory",
            extra={"error": str(exc)},
        )
        return InMemorySessionService(), "in-memory (fallback)"


class AgentEngine:
    """One agent, one runner, many sessions."""

    def __init__(self) -> None:
        cfg = get_config()
        self.config = cfg
        self.session_service, self.session_backend = build_session_service(cfg)
        self.agent = LlmAgent(
            name="personal_context_agent",
            model=cfg.model,
            description=(
                "Observes a person's context and acts on it: flags items missing "
                "before they leave, recalls where things were last seen, and "
                "reconstructs their day."
            ),
            instruction=SYSTEM_INSTRUCTION,
            tools=list(AGENT_TOOLS),
            generate_content_config=types.GenerateContentConfig(temperature=0.3),
        )
        self.runner = Runner(
            app_name=cfg.app_name,
            agent=self.agent,
            session_service=self.session_service,
        )
        log.info(
            "agent engine ready",
            extra={"model": cfg.model, "tools": len(AGENT_TOOLS), "sessions": self.session_backend},
        )

    async def _ensure_session(self, user_id: str, session_id: str, state: dict[str, Any]) -> None:
        existing = await self.session_service.get_session(
            app_name=self.config.app_name, user_id=user_id, session_id=session_id
        )
        if existing is None:
            await self.session_service.create_session(
                app_name=self.config.app_name, user_id=user_id,
                session_id=session_id, state=state,
            )

    async def handle(
        self,
        *,
        text: str,
        user_id: str | None = None,
        session_id: str | None = None,
        frame_data_url: str | None = None,
    ) -> AgentReply:
        """Run one conversational turn."""
        cfg = self.config
        user_id = user_id or cfg.default_user_id
        session_id = session_id or str(uuid.uuid4())
        cache = get_cache()

        # A frame that arrives with the turn is the freshest thing we have.
        if frame_data_url:
            await cache.put_frame(session_id, frame_data_url)

        session_state = await cache.get_session(session_id, user_id)
        intent: IntentResult = await classify(text)
        session_state.current_intent = str(intent.intent)
        if intent.destination:
            session_state.learned_context["last_destination"] = intent.destination

        if not cfg.genai_available:
            return await self._handle_without_model(text, intent, user_id, session_id)

        await self._ensure_session(
            user_id,
            session_id,
            {
                "user_id": user_id,
                "session_id": session_id,
                "current_location": session_state.current_location or "home",
            },
        )

        reply = AgentReply(
            text="", intent=str(intent.intent),
            intent_confidence=intent.confidence, session_id=session_id,
        )
        message = types.Content(role="user", parts=[types.Part.from_text(text=text)])

        try:
            async for event in self.runner.run_async(
                user_id=user_id, session_id=session_id, new_message=message
            ):
                content = getattr(event, "content", None)
                if not content or not content.parts:
                    continue
                for part in content.parts:
                    if getattr(part, "function_call", None):
                        call = part.function_call
                        reply.tool_calls.append({"name": call.name, "args": dict(call.args or {})})
                    elif getattr(part, "function_response", None):
                        response = part.function_response.response or {}
                        if isinstance(response, dict) and "workflow" in response:
                            reply.workflow_results.append(response)
                    elif getattr(part, "text", None) and not getattr(event, "partial", False):
                        reply.text += part.text
        except Exception as exc:  # noqa: BLE001 - classify, then re-raise
            if not is_quota_error(exc):
                raise
            wait = retry_after_seconds(exc)
            log.warning("gemini quota exhausted", extra={"retry_after": wait, "user_id": user_id})
            # A tool may already have run and persisted its work before the model
            # ran out of budget; surface that rather than throwing it away.
            if reply.workflow_results:
                reply.text = reply.workflow_results[-1].get("speech", "")
                reply.degraded = True
                await cache.save_session(session_state)
                return reply
            raise ModelQuotaError(
                f"Gemini is rate-limited right now. Try again in about {wait:.0f} seconds.",
                retry_after=wait,
            ) from exc

        reply.text = reply.text.strip()
        if not reply.text and reply.workflow_results:
            # The model called a tool but produced no prose; the workflow's own
            # phrasing is a better answer than silence.
            reply.text = reply.workflow_results[-1].get("speech", "")

        session_state.active_observations.append(
            {"utterance": text, "intent": str(intent.intent), "tools": [c["name"] for c in reply.tool_calls]}
        )
        await cache.save_session(session_state)

        log.info(
            "turn complete",
            extra={"intent": str(intent.intent), "tools": [c["name"] for c in reply.tool_calls],
                   "user_id": user_id, "session_id": session_id},
        )
        return reply

    async def _handle_without_model(
        self, text: str, intent: IntentResult, user_id: str, session_id: str
    ) -> AgentReply:
        """No Gemini credentials: run the matched workflow directly.

        The service stays useful (and testable) without a key — it just loses the
        conversational layer and the vision scan.
        """
        from agent.workflows import daily_timeline, item_recall, leave_detection

        reply = AgentReply(
            text="", intent=str(intent.intent), intent_confidence=intent.confidence,
            session_id=session_id, degraded=True,
        )
        if intent.intent is Intent.LEAVE_DETECTION:
            result = await leave_detection(
                user_id=user_id, session_id=session_id, destination=intent.destination or "out"
            )
        elif intent.intent is Intent.ITEM_RECALL and intent.item:
            result = await item_recall(user_id=user_id, item=intent.item)
        elif intent.intent is Intent.DAILY_TIMELINE:
            result = await daily_timeline(user_id=user_id, question=intent.time_reference)
        else:
            reply.text = (
                "I'm running without model credentials, so I can only handle direct "
                "requests: tell me you're leaving, ask where something is, or ask "
                "what you did today."
            )
            return reply

        payload = result.to_dict()
        reply.workflow_results.append(payload)
        reply.text = payload.get("speech", "")
        return reply


_engine: AgentEngine | None = None


def init_engine() -> AgentEngine:
    global _engine
    _engine = AgentEngine()
    return _engine


def get_engine() -> AgentEngine:
    if _engine is None:
        raise RuntimeError("engine not initialised — call init_engine() during startup")
    return _engine
