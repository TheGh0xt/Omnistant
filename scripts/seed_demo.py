"""Seed data for a demo.

Two modes, deliberately separated:

  --routines   Seeds only the *learned routines* — what you normally take to
               work, the gym, and so on. This is setup, not history: it is the
               equivalent of the agent having watched you for a fortnight.

  --sample-day Also inserts a fabricated day of observations so the timeline
               workflow has something to narrate before you have used the app.
               Every row it writes is tagged {"source": "seed"} so it can be told
               apart from real observations, and removed with --clear-seed.

For a submission demo, prefer real data: run `--routines`, then actually use the
app a few times. A judge can tell the difference, and so can the agent — real
runs produce the messy timings and partial scans that make the recall workflow
look like it is doing something.

    uv run python scripts/seed_demo.py --routines
    uv run python scripts/seed_demo.py --routines --sample-day
    uv run python scripts/seed_demo.py --clear-seed
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from utils.cache import close_cache, init_cache  # noqa: E402
from utils.config import get_config, tz  # noqa: E402
from utils.db import Observation, Routine, close_store, get_store, init_store  # noqa: E402
from utils.logger import configure_logging  # noqa: E402

ROUTINES = [
    ("work", ["phone", "wallet", "keys", "laptop", "airpods", "badge"], "office", "08:45", 12),
    ("gym", ["phone", "keys", "water bottle", "headphones", "towel"], "gym", "18:30", 6),
    ("shopping", ["phone", "wallet", "keys", "tote bag"], "high street", "11:00", 3),
]

# (hour, minute, kind, subject, location, detail)
SAMPLE_DAY = [
    (8, 12, "location", "home", "home", "woke up at home"),
    (8, 31, "item", "airpods", "kitchen counter", "on the kitchen counter"),
    (8, 33, "item", "wallet", "hall table", "on the hall table"),
    (8, 34, "item", "keys", "hall table", "next to the wallet"),
    (8, 47, "activity", "leaving home for work", "home", "left for the office"),
    (9, 21, "location", "office", "office", "arrived at the office"),
    (9, 24, "item", "laptop", "office desk", "set up at the desk"),
    (12, 30, "activity", "lunch", "dean street cafe", "lunch out"),
    (13, 45, "location", "office", "office", "back at the desk"),
    (17, 52, "activity", "leaving office for home", "office", "headed home"),
]


async def seed_routines(user_id: str) -> None:
    store = get_store()
    for name, items, location, typical, seen in ROUTINES:
        await store.upsert_routine(
            Routine(
                user_id=user_id, routine_name=name, expected_items=items,
                location_label=location, typical_time=typical, times_observed=seen,
            )
        )
        print(f"  routine {name:9} -> {', '.join(items)}")


async def seed_sample_day(user_id: str) -> None:
    store = get_store()
    today = datetime.now(tz()).date()
    batch = []
    for hour, minute, kind, subject, location, detail in SAMPLE_DAY:
        when = datetime.combine(today, time(hour, minute), tzinfo=tz())
        batch.append(
            Observation(
                user_id=user_id, observation_type=kind, subject=subject,
                content={"detail": detail, "source": "seed"},
                observed_at=when, location_label=location,
                confidence=0.9, verification_method="inferred",
            )
        )
    await store.add_observations(batch)
    print(f"  inserted {len(batch)} sample observations for {today.isoformat()}")
    print("  (tagged source=seed — these are FABRICATED, not real runs)")


async def clear_seed(user_id: str) -> None:
    """Remove only the fabricated rows, leaving real observations intact."""
    from utils.db import PostgresStore

    store = get_store()
    if not isinstance(store, PostgresStore):
        print("  clear-seed needs a real database (set DATABASE_URL).")
        return
    async with store._pool.connection() as conn:  # noqa: SLF001 - maintenance script
        cur = await conn.execute(
            "DELETE FROM observations WHERE user_id = %s AND content->>'source' = 'seed'",
            (user_id,),
        )
        await conn.commit()
        print(f"  deleted {cur.rowcount} seeded observations")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--routines", action="store_true", help="seed learned routines")
    parser.add_argument("--sample-day", action="store_true", help="also insert a fabricated day")
    parser.add_argument("--clear-seed", action="store_true", help="delete previously seeded observations")
    args = parser.parse_args()

    if not any((args.routines, args.sample_day, args.clear_seed)):
        parser.print_help()
        return 1

    configure_logging("WARNING")
    cfg = get_config()
    await init_store()
    await init_cache()
    user_id = cfg.default_user_id
    print(f"Seeding for user {user_id}")
    print(f"Storage: {cfg.report()['postgres']}\n")

    if args.clear_seed:
        await clear_seed(user_id)
    if args.routines:
        await seed_routines(user_id)
    if args.sample_day:
        await seed_sample_day(user_id)

    await close_store()
    await close_cache()
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
