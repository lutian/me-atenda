-- Separate from history/recordings; issuance is not confirmed playback.
CREATE TABLE IF NOT EXISTS call_answering (
    call_id BIGINT PRIMARY KEY REFERENCES calls(id),
    hello_issued_at TIMESTAMPTZ,
    verdict TEXT CHECK (verdict IN ('human','fax','unknown','machine_start',
        'machine_end_beep','machine_end_silence','machine_end_other')),
    polls INTEGER NOT NULL DEFAULT 0 CHECK (polls BETWEEN 0 AND 35)
);
