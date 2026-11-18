-- Call goal per product: steers drafts and replies and defines a successful call. Optional.
ALTER TABLE products ADD COLUMN IF NOT EXISTS goal TEXT
    CHECK (goal IS NULL OR char_length(goal) BETWEEN 1 AND 500);
