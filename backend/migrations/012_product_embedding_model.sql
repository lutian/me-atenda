-- Embedding model of each product's vectors. Products whose model differs from the one selected in
-- Settings are unavailable until their embeddings are updated. Runs once: existing vectors were all
-- produced with text-embedding-3-small, the only embedding model supported before the catalog.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
                   AND table_name = 'products' AND column_name = 'embedding_model_id') THEN
        ALTER TABLE products ADD COLUMN embedding_model_id INTEGER REFERENCES ai_models(id) ON DELETE SET NULL;
        UPDATE products SET embedding_model_id = (SELECT id FROM ai_models WHERE name = 'text-embedding-3-small');
    END IF;
END $$;
