"""PostgreSQL event log.

Two interchangeable implementations behind one interface:

  * `PostgresStore` — the real thing (Cloud SQL / local Postgres).
  * `MemoryStore`   — process-local fallback used when DATABASE_URL is unset, so
                      the service still boots for tests, CI and quick demos.

Both are append-only for observations: the agent's memory is a history, never a
mutable snapshot.  That is what makes "where was it last seen" answerable.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import get_config
from .logger import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

VALID_TYPES = {"item", "location", "activity"}
VALID_METHODS = {"visual", "voice", "manual", "inferred"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_subject(raw: str) -> str:
    """Canonical key for an observed thing.

    Recall has to survive the user saying "my AirPods" and the vision model
    saying "AirPods Pro case".  Lower-casing plus stripping possessives and
    articles gets most of the way; the ILIKE match in `find_by_subject` covers
    the rest.
    """
    text = (raw or "").strip().lower()
    for prefix in ("my ", "the ", "a ", "an ", "some "):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return " ".join(text.split()).rstrip("?.!,")


@dataclass
class Observation:
    user_id: str
    observation_type: str
    subject: str
    content: dict[str, Any] = field(default_factory=dict)
    observed_at: datetime = field(default_factory=utcnow)
    location_label: str | None = None
    lat: float | None = None
    lon: float | None = None
    confidence: float = 0.5
    verification_method: str = "manual"
    session_id: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def __post_init__(self) -> None:
        if self.observation_type not in VALID_TYPES:
            raise ValueError(
                f"observation_type must be one of {sorted(VALID_TYPES)}, got {self.observation_type!r}"
            )
        if self.verification_method not in VALID_METHODS:
            raise ValueError(
                f"verification_method must be one of {sorted(VALID_METHODS)}, "
                f"got {self.verification_method!r}"
            )
        self.confidence = max(0.0, min(1.0, float(self.confidence)))
        self.subject = normalize_subject(self.subject)
        if self.observed_at.tzinfo is None:
            self.observed_at = self.observed_at.replace(tzinfo=timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["observed_at"] = self.observed_at.isoformat()
        return data


@dataclass
class Routine:
    user_id: str
    routine_name: str
    expected_items: list[str]
    location_label: str | None = None
    typical_time: str | None = None
    times_observed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Store:
    """Interface both backends implement."""

    kind = "abstract"

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def healthy(self) -> bool: ...

    async def add_observation(self, obs: Observation) -> Observation: ...
    async def add_observations(self, batch: Sequence[Observation]) -> list[Observation]: ...
    async def find_by_subject(
        self,
        user_id: str,
        subject: str,
        limit: int = 20,
        types: Sequence[str] = ("item",),
    ) -> list[Observation]: ...
    async def observations_between(
        self, user_id: str, start: datetime, end: datetime, limit: int = 500
    ) -> list[Observation]: ...
    async def distinct_subjects(self, user_id: str, observation_type: str) -> list[str]: ...

    async def get_routine(self, user_id: str, name: str) -> Routine | None: ...
    async def list_routines(self, user_id: str) -> list[Routine]: ...
    async def upsert_routine(self, routine: Routine) -> Routine: ...

    async def record_leave_scan(self, **kwargs: Any) -> dict[str, Any]: ...
    async def enqueue_nudge(
        self, user_id: str, kind: str, due_at: datetime, payload: dict[str, Any]
    ) -> str: ...
    async def due_nudges(self, now: datetime, limit: int = 20) -> list[dict[str, Any]]: ...
    async def close_nudge(self, nudge_id: str, *, sent: bool) -> None: ...
    async def recent_leave_scans(self, user_id: str, limit: int = 10) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
class PostgresStore(Store):
    kind = "postgres"

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any = None

    async def connect(self) -> None:
        from psycopg_pool import AsyncConnectionPool

        self._pool = AsyncConnectionPool(self._dsn, min_size=1, max_size=8, open=False)
        await self._pool.open(wait=True, timeout=15)
        await self.apply_migrations()
        log.info("postgres connected", extra={"pool_max": 8})

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    async def healthy(self) -> bool:
        try:
            async with self._pool.connection() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception as exc:  # noqa: BLE001 - health probe must not raise
            log.warning("postgres health check failed", extra={"error": str(exc)})
            return False

    async def apply_migrations(self) -> None:
        """Run every migrations/*.sql in name order. All statements are idempotent."""
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        async with self._pool.connection() as conn:
            for path in files:
                await conn.execute(path.read_text())
            await conn.commit()
        log.info("migrations applied", extra={"files": [f.name for f in files]})

    @staticmethod
    def _row_to_obs(row: tuple) -> Observation:
        (
            oid, user_id, otype, subject, content, observed_at,
            location_label, lat, lon, confidence, method, session_id,
        ) = row
        return Observation(
            id=str(oid),
            user_id=str(user_id),
            observation_type=otype,
            subject=subject,
            content=content or {},
            observed_at=observed_at,
            location_label=location_label,
            lat=lat,
            lon=lon,
            confidence=confidence,
            verification_method=method,
            session_id=str(session_id) if session_id else None,
        )

    _SELECT = """
        SELECT id, user_id, observation_type, subject, content, observed_at,
               location_label, lat, lon, confidence, verification_method, session_id
        FROM observations
    """

    async def add_observation(self, obs: Observation) -> Observation:
        (result,) = await self.add_observations([obs])
        return result

    async def add_observations(self, batch: Sequence[Observation]) -> list[Observation]:
        if not batch:
            return []
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                for obs in batch:
                    await cur.execute(
                        """
                        INSERT INTO observations
                            (id, user_id, observation_type, subject, content, observed_at,
                             location_label, lat, lon, confidence, verification_method, session_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (
                            obs.id, obs.user_id, obs.observation_type, obs.subject,
                            json.dumps(obs.content), obs.observed_at, obs.location_label,
                            obs.lat, obs.lon, obs.confidence, obs.verification_method,
                            obs.session_id,
                        ),
                    )
            await conn.commit()
        return list(batch)

    async def find_by_subject(
        self,
        user_id: str,
        subject: str,
        limit: int = 20,
        types: Sequence[str] = ("item",),
    ) -> list[Observation]:
        """Find sightings of a thing.

        Restricted to `types` (item observations by default) and matched against
        the subject and the human-readable detail only — deliberately NOT against
        the whole JSONB blob. A leave scan stores the names of everything that was
        *missing* in its content; a blob-wide match would return those rows and we
        would report an item as "last seen" at the exact moment we established it
        was nowhere to be found.
        """
        term = normalize_subject(subject)
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    self._SELECT
                    + """
                    WHERE user_id = %s
                      AND observation_type = ANY(%s)
                      AND (subject ILIKE %s
                           OR %s ILIKE '%%' || subject || '%%'
                           OR COALESCE(content->>'detail', '') ILIKE %s)
                    ORDER BY observed_at DESC
                    LIMIT %s
                    """,
                    (user_id, list(types), f"%{term}%", term, f"%{term}%", limit),
                )
                rows = await cur.fetchall()
        return [self._row_to_obs(r) for r in rows]

    async def observations_between(
        self, user_id: str, start: datetime, end: datetime, limit: int = 500
    ) -> list[Observation]:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    self._SELECT
                    + """
                    WHERE user_id = %s AND observed_at >= %s AND observed_at < %s
                    ORDER BY observed_at ASC
                    LIMIT %s
                    """,
                    (user_id, start, end, limit),
                )
                rows = await cur.fetchall()
        return [self._row_to_obs(r) for r in rows]

    async def distinct_subjects(self, user_id: str, observation_type: str) -> list[str]:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT subject, COUNT(*) AS n FROM observations
                    WHERE user_id = %s AND observation_type = %s
                    GROUP BY subject ORDER BY n DESC
                    """,
                    (user_id, observation_type),
                )
                return [r[0] for r in await cur.fetchall()]

    async def get_routine(self, user_id: str, name: str) -> Routine | None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT user_id, routine_name, expected_items, location_label,
                           typical_time, times_observed
                    FROM routines WHERE user_id = %s AND routine_name = %s
                    """,
                    (user_id, name.lower()),
                )
                row = await cur.fetchone()
        if not row:
            return None
        return Routine(
            user_id=str(row[0]), routine_name=row[1], expected_items=list(row[2] or []),
            location_label=row[3], typical_time=row[4].isoformat() if row[4] else None,
            times_observed=row[5],
        )

    async def list_routines(self, user_id: str) -> list[Routine]:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT user_id, routine_name, expected_items, location_label,
                           typical_time, times_observed
                    FROM routines WHERE user_id = %s ORDER BY routine_name
                    """,
                    (user_id,),
                )
                rows = await cur.fetchall()
        return [
            Routine(
                user_id=str(r[0]), routine_name=r[1], expected_items=list(r[2] or []),
                location_label=r[3], typical_time=r[4].isoformat() if r[4] else None,
                times_observed=r[5],
            )
            for r in rows
        ]

    async def upsert_routine(self, routine: Routine) -> Routine:
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO routines
                    (user_id, routine_name, expected_items, location_label,
                     typical_time, times_observed)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id, routine_name) DO UPDATE SET
                    expected_items = EXCLUDED.expected_items,
                    location_label = COALESCE(EXCLUDED.location_label, routines.location_label),
                    typical_time   = COALESCE(EXCLUDED.typical_time, routines.typical_time),
                    times_observed = EXCLUDED.times_observed,
                    updated_at     = now()
                """,
                (
                    routine.user_id, routine.routine_name.lower(),
                    json.dumps(routine.expected_items), routine.location_label,
                    routine.typical_time, routine.times_observed,
                ),
            )
            await conn.commit()
        return routine

    async def record_leave_scan(self, **kw: Any) -> dict[str, Any]:
        record = _leave_scan_record(kw)
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO leave_scans
                    (id, user_id, routine_name, found_items, missing_items,
                     extra_items, verdict, scanned_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record["id"], record["user_id"], record["routine_name"],
                    json.dumps(record["found_items"]), json.dumps(record["missing_items"]),
                    json.dumps(record["extra_items"]), record["verdict"], record["scanned_at"],
                ),
            )
            await conn.commit()
        return record

    async def recent_leave_scans(self, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, user_id, routine_name, found_items, missing_items,
                           extra_items, verdict, scanned_at
                    FROM leave_scans WHERE user_id = %s
                    ORDER BY scanned_at DESC LIMIT %s
                    """,
                    (user_id, limit),
                )
                rows = await cur.fetchall()
        return [
            {
                "id": str(r[0]), "user_id": str(r[1]), "routine_name": r[2],
                "found_items": r[3], "missing_items": r[4], "extra_items": r[5],
                "verdict": r[6], "scanned_at": r[7],
            }
            for r in rows
        ]


    async def enqueue_nudge(
        self, user_id: str, kind: str, due_at: datetime, payload: dict[str, Any]
    ) -> str:
        nudge_id = str(uuid.uuid4())
        async with self._pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO pending_nudges (id, user_id, kind, due_at, payload)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (nudge_id, user_id, kind, due_at, json.dumps(payload)),
            )
            await conn.commit()
        return nudge_id

    async def due_nudges(self, now: datetime, limit: int = 20) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, user_id, kind, due_at, payload
                    FROM pending_nudges
                    WHERE sent_at IS NULL AND cancelled_at IS NULL AND due_at <= %s
                    ORDER BY due_at
                    LIMIT %s
                    """,
                    (now, limit),
                )
                rows = await cur.fetchall()
        return [
            {"id": str(r[0]), "user_id": str(r[1]), "kind": r[2], "due_at": r[3], "payload": r[4] or {}}
            for r in rows
        ]

    async def close_nudge(self, nudge_id: str, *, sent: bool) -> None:
        column = "sent_at" if sent else "cancelled_at"
        async with self._pool.connection() as conn:
            await conn.execute(
                f"UPDATE pending_nudges SET {column} = now() WHERE id = %s", (nudge_id,)
            )
            await conn.commit()


def _leave_scan_record(kw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": kw.get("id") or str(uuid.uuid4()),
        "user_id": kw["user_id"],
        "routine_name": kw["routine_name"],
        "found_items": list(kw.get("found_items") or []),
        "missing_items": list(kw.get("missing_items") or []),
        "extra_items": list(kw.get("extra_items") or []),
        "verdict": kw.get("verdict", ""),
        "scanned_at": kw.get("scanned_at") or utcnow(),
    }


# ---------------------------------------------------------------------------
# In-memory fallback
# ---------------------------------------------------------------------------
class MemoryStore(Store):
    kind = "memory"

    def __init__(self) -> None:
        self._obs: list[Observation] = []
        self._routines: dict[tuple[str, str], Routine] = {}
        self._scans: list[dict[str, Any]] = []
        self._nudges: list[dict[str, Any]] = []

    async def connect(self) -> None:
        log.warning("DATABASE_URL unset — using in-memory store (data is lost on restart)")

    async def close(self) -> None:
        return None

    async def healthy(self) -> bool:
        return True

    async def add_observation(self, obs: Observation) -> Observation:
        self._obs.append(obs)
        return obs

    async def add_observations(self, batch: Sequence[Observation]) -> list[Observation]:
        self._obs.extend(batch)
        return list(batch)

    def _for_user(self, user_id: str) -> Iterable[Observation]:
        return (o for o in self._obs if o.user_id == user_id)

    async def find_by_subject(
        self,
        user_id: str,
        subject: str,
        limit: int = 20,
        types: Sequence[str] = ("item",),
    ) -> list[Observation]:
        term = normalize_subject(subject)
        allowed = set(types)
        hits = [
            o
            for o in self._for_user(user_id)
            if o.observation_type in allowed
            and (
                term in o.subject
                or o.subject in term
                or term in str((o.content or {}).get("detail", "")).lower()
            )
        ]
        hits.sort(key=lambda o: o.observed_at, reverse=True)
        return hits[:limit]

    async def observations_between(
        self, user_id: str, start: datetime, end: datetime, limit: int = 500
    ) -> list[Observation]:
        hits = [o for o in self._for_user(user_id) if start <= o.observed_at < end]
        hits.sort(key=lambda o: o.observed_at)
        return hits[:limit]

    async def distinct_subjects(self, user_id: str, observation_type: str) -> list[str]:
        counts: dict[str, int] = {}
        for o in self._for_user(user_id):
            if o.observation_type == observation_type:
                counts[o.subject] = counts.get(o.subject, 0) + 1
        return sorted(counts, key=lambda s: counts[s], reverse=True)

    async def get_routine(self, user_id: str, name: str) -> Routine | None:
        return self._routines.get((user_id, name.lower()))

    async def list_routines(self, user_id: str) -> list[Routine]:
        return [r for (uid, _), r in self._routines.items() if uid == user_id]

    async def upsert_routine(self, routine: Routine) -> Routine:
        self._routines[(routine.user_id, routine.routine_name.lower())] = routine
        return routine

    async def record_leave_scan(self, **kw: Any) -> dict[str, Any]:
        record = _leave_scan_record(kw)
        self._scans.append(record)
        return record

    async def enqueue_nudge(
        self, user_id: str, kind: str, due_at: datetime, payload: dict[str, Any]
    ) -> str:
        nudge_id = str(uuid.uuid4())
        self._nudges.append({
            "id": nudge_id, "user_id": user_id, "kind": kind,
            "due_at": due_at, "payload": payload, "sent_at": None, "cancelled_at": None,
        })
        return nudge_id

    async def due_nudges(self, now: datetime, limit: int = 20) -> list[dict[str, Any]]:
        due = [
            n for n in self._nudges
            if n["sent_at"] is None and n["cancelled_at"] is None and n["due_at"] <= now
        ]
        due.sort(key=lambda n: n["due_at"])
        return due[:limit]

    async def close_nudge(self, nudge_id: str, *, sent: bool) -> None:
        for n in self._nudges:
            if n["id"] == nudge_id:
                n["sent_at" if sent else "cancelled_at"] = utcnow()

    async def recent_leave_scans(self, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        hits = [s for s in self._scans if s["user_id"] == user_id]
        hits.sort(key=lambda s: s["scanned_at"], reverse=True)
        return hits[:limit]


_store: Store | None = None


async def init_store() -> Store:
    global _store
    cfg = get_config()
    _store = PostgresStore(cfg.database_url) if cfg.postgres_enabled else MemoryStore()
    await _store.connect()
    return _store


def get_store() -> Store:
    if _store is None:
        raise RuntimeError("store not initialised — call init_store() during startup")
    return _store


async def close_store() -> None:
    global _store
    if _store is not None:
        await _store.close()
        _store = None
