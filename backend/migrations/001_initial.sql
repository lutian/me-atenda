CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS products (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS product_chunks (
    id BIGSERIAL PRIMARY KEY,
    product_id BIGINT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    embedding vector(1536) NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL CHECK (phone ~ '^\+[1-9][0-9]{7,14}$'),
    company TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pendente'
);
CREATE TABLE IF NOT EXISTS calls (
    id BIGSERIAL PRIMARY KEY,
    lead_id BIGINT NOT NULL REFERENCES leads(id),
    product_id BIGINT NOT NULL REFERENCES products(id),
    sid TEXT UNIQUE,
    stream_sid TEXT UNIQUE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    human BOOLEAN NOT NULL DEFAULT FALSE,
    status_final TEXT NOT NULL DEFAULT 'ligando' CHECK (status_final IN
        ('pendente','ligando','sucesso','sem_interesse','caixa_postal','numero_errado',
         'atendimento_automatico','falha_tecnica','inconclusivo')),
    duration_seconds INTEGER NOT NULL DEFAULT 0 CHECK (duration_seconds >= 0),
    transcription_json JSONB NOT NULL DEFAULT '[]',
    recording_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (recording_status IN ('pending','failed','available','absent')),
    recording_sid TEXT,
    audio BYTEA,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- A database-level invariant, valid across processes, workers and restarts.
CREATE UNIQUE INDEX IF NOT EXISTS one_active_call ON calls ((TRUE)) WHERE active;
