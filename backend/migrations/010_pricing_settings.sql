-- Rates edited in Settings. When present they replace OBSERVABILITY_PRICING_JSON for new events;
-- each event and call keeps its own pricing snapshot, so saving never reprices history.
CREATE TABLE IF NOT EXISTS pricing_settings (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    config JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
