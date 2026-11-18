-- Operator-managed AI models per option and their costs (USD). Settings picks one model per option.
CREATE TABLE IF NOT EXISTS ai_models (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (name ~ '^[A-Za-z0-9_.:/-]{1,120}$'),
    kind TEXT NOT NULL CHECK (kind IN ('text', 'audio', 'embedding')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS model_costs (
    model_id INTEGER PRIMARY KEY REFERENCES ai_models(id) ON DELETE CASCADE,
    input_per_million NUMERIC CHECK (input_per_million >= 0),
    cached_input_per_million NUMERIC CHECK (cached_input_per_million >= 0),
    output_per_million NUMERIC CHECK (output_per_million >= 0),
    characters_per_million NUMERIC CHECK (characters_per_million >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One model in use per option; a model in use cannot be deleted.
CREATE TABLE IF NOT EXISTS active_models (
    kind TEXT PRIMARY KEY CHECK (kind IN ('text', 'audio', 'embedding')),
    model_id INTEGER NOT NULL REFERENCES ai_models(id) ON DELETE RESTRICT
);
-- Initial catalog, only on an empty table so deleted models never come back on restart.
-- Prices: developers.openai.com/api/docs/pricing (standard tier, short context), read 2026-11-18.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ai_models) THEN
        INSERT INTO ai_models(name, kind) VALUES
            ('gpt-6.1-sol', 'text'), ('gpt-6.2-sol', 'text'),
            ('text-embedding-3-small', 'embedding'), ('tts-1', 'audio');
        INSERT INTO model_costs(model_id, input_per_million, cached_input_per_million, output_per_million)
            SELECT id, 2.00, 0.10, 10.00 FROM ai_models WHERE name IN ('gpt-6.1-sol', 'gpt-6.2-sol');
        INSERT INTO model_costs(model_id, input_per_million)
            SELECT id, 0.02 FROM ai_models WHERE name = 'text-embedding-3-small';
        INSERT INTO model_costs(model_id, characters_per_million)
            SELECT id, 15.00 FROM ai_models WHERE name = 'tts-1';
    END IF;
END $$;
