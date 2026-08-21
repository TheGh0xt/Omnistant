-- Personal Context Agent — event log schema.
--
-- Design notes (deviations from the original spec are deliberate and marked):
--
--  * `location` is stored as (location_label, lat, lon) rather than a bare
--    PostgreSQL POINT.  Every workflow keys off the *human* label ("home",
--    "office") — that is what the agent says back to the user — and core PG's
--    geometric `point` type has no distance index without PostGIS.  A generated
--    `location` POINT column is kept for spec fidelity / future geo queries.
--
--  * `subject` is a normalised, lower-cased name of the thing observed
--    ("airpods", "office", "lunch").  It exists so item recall is an index scan
--    instead of a JSONB scan; the full detail still lives in `content`.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- gen_random_uuid()

-- --------------------------------------------------------------------------
-- Append-only observation log.  Nothing in this table is ever updated: the
-- agent's memory is the ordered history of what it saw, not a mutable snapshot.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS observations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL,
    observation_type    TEXT NOT NULL
                        CHECK (observation_type IN ('item', 'location', 'activity')),
    subject             TEXT NOT NULL,
    content             JSONB NOT NULL DEFAULT '{}'::jsonb,
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    location_label      TEXT,
    lat                 DOUBLE PRECISION,
    lon                 DOUBLE PRECISION,
    location            POINT GENERATED ALWAYS AS (
                            CASE WHEN lat IS NOT NULL AND lon IS NOT NULL
                                 THEN point(lon, lat) END
                        ) STORED,
    confidence          REAL NOT NULL DEFAULT 0.5
                        CHECK (confidence >= 0 AND confidence <= 1),
    verification_method TEXT NOT NULL DEFAULT 'manual'
                        CHECK (verification_method IN ('visual', 'voice', 'manual', 'inferred')),
    session_id          UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Workflow 3 (timeline) sweeps one user's day in chronological order.
CREATE INDEX IF NOT EXISTS observations_user_time_idx
    ON observations (user_id, observed_at DESC);

-- Workflow 2 (recall) asks "when did I last see <subject>?".
CREATE INDEX IF NOT EXISTS observations_user_subject_time_idx
    ON observations (user_id, subject, observed_at DESC);

CREATE INDEX IF NOT EXISTS observations_content_idx
    ON observations USING GIN (content);

-- --------------------------------------------------------------------------
-- Learned routines: "when you go to work you normally carry these things".
-- Seeded from a handful of trips, then refined by leave-scan outcomes.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS routines (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL,
    routine_name    TEXT NOT NULL,
    expected_items  JSONB NOT NULL DEFAULT '[]'::jsonb,
    location_label  TEXT,
    lat             DOUBLE PRECISION,
    lon             DOUBLE PRECISION,
    location        POINT GENERATED ALWAYS AS (
                        CASE WHEN lat IS NOT NULL AND lon IS NOT NULL
                             THEN point(lon, lat) END
                    ) STORED,
    typical_time    TIME,
    times_observed  INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (user_id, routine_name)
);

-- --------------------------------------------------------------------------
-- Leave-scan audit trail.  Keeping the result of every scan is what lets the
-- agent learn ("you have taken the charger on 6 of your last 7 work trips")
-- and gives the demo real, non-fabricated history.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS leave_scans (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL,
    routine_name    TEXT NOT NULL,
    found_items     JSONB NOT NULL DEFAULT '[]'::jsonb,
    missing_items   JSONB NOT NULL DEFAULT '[]'::jsonb,
    extra_items     JSONB NOT NULL DEFAULT '[]'::jsonb,
    verdict         TEXT NOT NULL,
    scanned_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS leave_scans_user_time_idx
    ON leave_scans (user_id, scanned_at DESC);
