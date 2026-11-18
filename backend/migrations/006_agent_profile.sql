-- Operator-chosen agent identity. NULL voice means the deployment default (OPENAI_TTS_VOICE).
ALTER TABLE call_policy ADD COLUMN IF NOT EXISTS agent_name TEXT NOT NULL DEFAULT 'Bia'
    CHECK (char_length(agent_name) BETWEEN 1 AND 40);
ALTER TABLE call_policy ADD COLUMN IF NOT EXISTS tts_voice TEXT
    CHECK (tts_voice IS NULL OR tts_voice ~ '^[a-z]{1,20}$');
-- Per-call snapshot; NULL on historic calls means the values in force were not recorded.
ALTER TABLE calls ADD COLUMN IF NOT EXISTS agent_name TEXT;
ALTER TABLE calls ADD COLUMN IF NOT EXISTS tts_voice TEXT;
