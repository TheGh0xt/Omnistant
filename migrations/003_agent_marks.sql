-- "Have we already done this today?"
--
-- Cloud Run scales to zero and the cache is allowed to be lossy, so neither can
-- answer that. A brief that fires twice because a container cold-started is
-- exactly the noise this product exists to avoid, so the answer lives in
-- Postgres and the primary key does the work: claiming a mark is an insert that
-- either succeeds once or conflicts.
CREATE TABLE IF NOT EXISTS agent_marks (
    user_id    UUID NOT NULL,
    mark_key   TEXT NOT NULL,
    marked_on  DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, mark_key, marked_on)
);
