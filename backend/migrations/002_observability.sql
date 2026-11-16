-- Additive upgrade: existing 001 tables, active-slot and terminal records are untouched.
CREATE TABLE IF NOT EXISTS integration_events (
    id BIGSERIAL PRIMARY KEY,
    call_id BIGINT REFERENCES calls(id),
    payload JSONB NOT NULL CHECK (payload->>'status' IN ('success','error','cancelled')),
    finalized BOOLEAN NOT NULL DEFAULT FALSE,
    stable_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS integration_events_call ON integration_events(call_id, id);
CREATE TABLE IF NOT EXISTS call_accounting (
    call_id BIGINT PRIMARY KEY REFERENCES calls(id),
    pricing_snapshot JSONB NOT NULL,
    billed_connectivity JSONB
);
