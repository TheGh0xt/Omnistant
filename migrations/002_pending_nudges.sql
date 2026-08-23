-- Delayed, self-cancelling reminders.
--
-- A fixed 08:00 brief assumes you leave at 08:00. The useful moment is a few
-- minutes *after* you actually walk out — late enough that you have gone, early
-- enough that turning back is still cheap.
--
-- Cloud Run scales to zero, so nothing can sit in memory waiting. The due time
-- goes in a table and a frequent scheduler job drains whatever has come due.
CREATE TABLE IF NOT EXISTS pending_nudges (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL,
    kind          TEXT NOT NULL,
    due_at        TIMESTAMPTZ NOT NULL,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at       TIMESTAMPTZ,
    cancelled_at  TIMESTAMPTZ
);

-- The drain query is "what is due and still outstanding", run every few minutes.
CREATE INDEX IF NOT EXISTS pending_nudges_due_idx
    ON pending_nudges (due_at)
    WHERE sent_at IS NULL AND cancelled_at IS NULL;
