-- Persist operator policy once; reapplying migrations must not reset it.
CREATE TABLE IF NOT EXISTS call_policy (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    max_duration_seconds INTEGER NOT NULL DEFAULT 180
        CHECK (max_duration_seconds BETWEEN 1 AND 14400)
);
INSERT INTO call_policy(id) VALUES(TRUE) ON CONFLICT DO NOTHING;
-- NULL means unknown for historic calls; never invent a retroactive carrier cap.
ALTER TABLE calls ADD COLUMN IF NOT EXISTS max_duration_seconds INTEGER
    CHECK (max_duration_seconds BETWEEN 1 AND 14400);
