-- Immutable exact-input speech; pending/failed entries require explicit operator reconciliation.
CREATE TABLE IF NOT EXISTS opening_audio (
    digest TEXT PRIMARY KEY,
    text TEXT NOT NULL,
    model TEXT NOT NULL,
    voice TEXT NOT NULL,
    format TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','ready')),
    event_floor BIGINT NOT NULL,
    audio BYTEA,
    CHECK ((state='pending' AND audio IS NULL) OR
           (state='ready' AND octet_length(audio) BETWEEN 1 AND 720000))
);
CREATE TABLE IF NOT EXISTS call_openings (
    call_id BIGINT PRIMARY KEY REFERENCES calls(id),
    hello TEXT NOT NULL REFERENCES opening_audio(digest),
    intro TEXT NOT NULL REFERENCES opening_audio(digest)
);
