-- Transcription (speech recognition) becomes a catalog option, priced per audio minute.
ALTER TABLE ai_models DROP CONSTRAINT IF EXISTS ai_models_kind_check;
ALTER TABLE ai_models ADD CONSTRAINT ai_models_kind_check
    CHECK (kind IN ('text', 'audio', 'embedding', 'transcription'));
ALTER TABLE active_models DROP CONSTRAINT IF EXISTS active_models_kind_check;
ALTER TABLE active_models ADD CONSTRAINT active_models_kind_check
    CHECK (kind IN ('text', 'audio', 'embedding', 'transcription'));
ALTER TABLE model_costs ADD COLUMN IF NOT EXISTS per_minute NUMERIC CHECK (per_minute >= 0);
-- Initial transcription models, only while the option has none (one in use can never be deleted).
-- Prices: developers.openai.com/api/docs/pricing, estimated cost per minute, read 2026-11-18.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ai_models WHERE kind = 'transcription') THEN
        INSERT INTO ai_models(name, kind) VALUES
            ('whisper-1', 'transcription'), ('gpt-4o-transcribe', 'transcription'),
            ('gpt-4o-mini-transcribe', 'transcription'), ('gpt-transcribe', 'transcription')
            ON CONFLICT (name) DO NOTHING;
        INSERT INTO model_costs(model_id, per_minute)
            SELECT id, CASE name WHEN 'gpt-4o-mini-transcribe' THEN 0.003 WHEN 'gpt-transcribe' THEN 0.0045
                                 ELSE 0.006 END
            FROM ai_models WHERE kind = 'transcription'
            ON CONFLICT (model_id) DO NOTHING;
    END IF;
END $$;
