-- Operator-confirmed scripted messages per product. Editing the product deletes them (stale pitch).
CREATE TABLE IF NOT EXISTS product_messages (
    product_id BIGINT PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    pitch TEXT NOT NULL CHECK (char_length(pitch) BETWEEN 1 AND 400),
    goodbye TEXT NOT NULL CHECK (char_length(goodbye) BETWEEN 1 AND 200)
);
-- NULL on historic calls and on calls dialed before messages were prepared.
ALTER TABLE call_openings ADD COLUMN IF NOT EXISTS pitch TEXT REFERENCES opening_audio(digest);
ALTER TABLE call_openings ADD COLUMN IF NOT EXISTS goodbye TEXT REFERENCES opening_audio(digest);
