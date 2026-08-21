"""Redis-backed session state (Memorystore in production).

Holds the things that are hot and short-lived — who is mid-conversation, where
they are, what the camera just saw — so the agent does not pay a Postgres round
trip on every turn.  Anything worth remembering tomorrow goes to the event log
instead; this layer is allowed to be lossy.

Falls back to a process-local dict when REDIS_URL is unset.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .config import get_config
from .logger import get_logger

log = get_logger(__name__)


@dataclass
class SessionState:
    """The spec's Redis-backed session document."""

    session_id: str
    user_id: str
    current_location: str | None = None
    lat: float | None = None
    lon: float | None = None
    current_intent: str | None = None
    active_observations: list[dict[str, Any]] = field(default_factory=list)
    learned_context: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class _Backend:
    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, ttl: int) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


class RedisBackend(_Backend):
    kind = "redis"

    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis

        self._client = aioredis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> str | None:
        return await self._client.get(key)

    async def set(self, key: str, value: str, ttl: int) -> None:
        await self._client.set(key, value, ex=ttl)

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except Exception as exc:  # noqa: BLE001 - health probe must not raise
            log.warning("redis ping failed", extra={"error": str(exc)})
            return False

    async def close(self) -> None:
        await self._client.aclose()


class MemoryBackend(_Backend):
    kind = "memory"

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}

    async def get(self, key: str) -> str | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires = entry
        if expires < time.time():
            self._data.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        self._data[key] = (value, time.time() + ttl)

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()


class Cache:
    """Typed helpers over whichever backend is active."""

    def __init__(self, backend: _Backend) -> None:
        self.backend = backend
        cfg = get_config()
        self._session_ttl = cfg.session_ttl_seconds
        self._frame_ttl = cfg.frame_ttl_seconds

    @property
    def kind(self) -> str:
        return getattr(self.backend, "kind", "unknown")

    # --- sessions ----------------------------------------------------------
    async def get_session(self, session_id: str, user_id: str) -> SessionState:
        raw = await self.backend.get(f"session:{session_id}")
        if raw:
            try:
                return SessionState.from_dict(json.loads(raw))
            except (json.JSONDecodeError, TypeError) as exc:
                log.warning("discarding corrupt session", extra={"session_id": session_id, "error": str(exc)})
        return SessionState(session_id=session_id, user_id=user_id)

    async def save_session(self, state: SessionState) -> None:
        state.updated_at = time.time()
        # Keep the working set bounded; the durable copy is in Postgres.
        state.active_observations = state.active_observations[-50:]
        await self.backend.set(
            f"session:{state.session_id}", json.dumps(state.to_dict(), default=str), self._session_ttl
        )

    # --- camera frames -----------------------------------------------------
    async def put_frame(self, session_id: str, data_url: str) -> None:
        """Stash the most recent camera frame so a tool call can pick it up.

        Frames are bulky and worthless after a couple of minutes, which is
        exactly what a TTL cache is for.
        """
        await self.backend.set(f"frame:{session_id}", data_url, self._frame_ttl)

    async def get_frame(self, session_id: str) -> str | None:
        return await self.backend.get(f"frame:{session_id}")

    async def clear_frame(self, session_id: str) -> None:
        await self.backend.delete(f"frame:{session_id}")

    # --- last-seen index ---------------------------------------------------
    async def note_last_seen(self, user_id: str, subject: str, payload: dict[str, Any]) -> None:
        """Fast path for "where are my X" — avoids hitting Postgres for hot items."""
        await self.backend.set(
            f"lastseen:{user_id}:{subject}", json.dumps(payload, default=str), self._session_ttl
        )

    async def get_last_seen(self, user_id: str, subject: str) -> dict[str, Any] | None:
        raw = await self.backend.get(f"lastseen:{user_id}:{subject}")
        return json.loads(raw) if raw else None

    async def healthy(self) -> bool:
        return await self.backend.ping()


_cache: Cache | None = None


async def init_cache() -> Cache:
    global _cache
    cfg = get_config()
    if cfg.redis_enabled:
        backend: _Backend = RedisBackend(cfg.redis_url)  # type: ignore[arg-type]
        if not await backend.ping():
            log.warning("redis unreachable — falling back to in-process cache")
            backend = MemoryBackend()
    else:
        log.warning("REDIS_URL unset — using in-process cache")
        backend = MemoryBackend()
    _cache = Cache(backend)
    return _cache


def get_cache() -> Cache:
    if _cache is None:
        raise RuntimeError("cache not initialised — call init_cache() during startup")
    return _cache


async def close_cache() -> None:
    global _cache
    if _cache is not None:
        await _cache.backend.close()
        _cache = None


def new_session_id() -> str:
    return str(uuid.uuid4())
