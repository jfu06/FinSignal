-- FinSignal Phase-1 schema (design-doc Section 7).
-- Target: Neon Postgres with the pgvector extension.
--
-- IMPORTANT: the vector(384) dimension below must match EMBEDDING_DIM and the
-- embedding model in .env (all-MiniLM-L6-v2 -> 384). If you change the
-- embedding model, change the dimension here and re-ingest.

CREATE EXTENSION IF NOT EXISTS vector;

-- 1) Chunk (Neon, pgvector): the narrative corpus + its embeddings.
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   TEXT PRIMARY KEY,          -- e.g. "AAPL_10K_0001"
    doc_id     TEXT NOT NULL,             -- e.g. "AAPL_10K_2023"
    ticker     TEXT NOT NULL,             -- e.g. "AAPL"
    section    TEXT,                       -- filing section/heading, may be NULL
    text       TEXT NOT NULL,
    embedding  vector(384) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chunks_ticker_idx ON chunks (ticker);

-- NO ANN index at this corpus size: exact scan over ~800 rows is fast and
-- always correct. An ivfflat index built before/while the table fills has
-- stale centroids and can silently return FEWER rows than LIMIT k. If the
-- corpus grows to many thousands of chunks, add (AFTER ingest, then REINDEX
-- on re-ingest):
--   CREATE INDEX chunks_embedding_cosine_idx
--       ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- 2) Claim: one row per generated claim, with its citations and judge verdict.
CREATE TABLE IF NOT EXISTS claims (
    claim_id        TEXT PRIMARY KEY,     -- e.g. "q123_claim1"
    query_id        TEXT NOT NULL,        -- groups claims of one report
    text            TEXT NOT NULL,
    cited_chunk_ids TEXT[] NOT NULL DEFAULT '{}',
    verdict         TEXT,                  -- SUPPORTED | NOT_ENOUGH_INFO | CONTRADICTED
    judge_reason    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT claims_verdict_chk
        CHECK (verdict IS NULL OR verdict IN
               ('SUPPORTED', 'NOT_ENOUGH_INFO', 'CONTRADICTED'))
);

CREATE INDEX IF NOT EXISTS claims_query_id_idx ON claims (query_id);

-- 3) TestCase: the mini golden set for the eval / release gate.
CREATE TABLE IF NOT EXISTS test_cases (
    case_id                 TEXT PRIMARY KEY,
    ticker                  TEXT NOT NULL,
    question                TEXT NOT NULL,
    expected_answer_snippet TEXT,
    expected_chunk_id       TEXT
);
