-- Reviewed opening per product, plus the agent name it mentions (a renamed agent needs a new opening).
-- NULL on rows saved before this migration: dialing falls back to the built-in opening template.
ALTER TABLE product_messages ADD COLUMN IF NOT EXISTS opening TEXT
    CHECK (opening IS NULL OR char_length(opening) BETWEEN 1 AND 400);
ALTER TABLE product_messages ADD COLUMN IF NOT EXISTS agent_name TEXT;
