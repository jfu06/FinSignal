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
    corpus     TEXT NOT NULL DEFAULT 'live',  -- 'live' | 'benchmark'
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS chunks_ticker_idx ON chunks (ticker);

-- Corpus isolation (idempotent migration for pre-existing databases):
-- 'live' rows serve the product; 'benchmark' rows hold historical filings for
-- the external FinanceBench suite and must never surface in ticker-scoped
-- retrieval (retrieval.py and _known_tickers filter on corpus = 'live').
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS corpus TEXT NOT NULL DEFAULT 'live';

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

-- ============================================================
-- Phase 2: numeric line (SEC XBRL companyfacts).
-- Schema follows docs/data-dictionary.md §6.
-- ============================================================

-- Raw facts, one row per reported data point. Re-ingest replaces a company
-- wholesale (DELETE by cik + INSERT), so no upsert key gymnastics with the
-- nullable start_date (instant concepts have no period start, §5.5).
CREATE TABLE IF NOT EXISTS xbrl_facts (
    cik        BIGINT  NOT NULL,
    taxonomy   TEXT    NOT NULL,   -- us-gaap | dei | ifrs-full | srt
    tag        TEXT    NOT NULL,
    unit       TEXT    NOT NULL,   -- USD | shares | USD/shares | pure | ...
    start_date DATE,               -- NULL for instant (balance-sheet) concepts
    end_date   DATE    NOT NULL,
    val        NUMERIC NOT NULL,   -- raw units: dollars are dollars (§0)
    accn       TEXT    NOT NULL,   -- accession number -> provenance link
    fy         INT,                -- filing's fiscal year, NOT the data's (§5.2)
    fp         TEXT,
    form       TEXT,               -- 10-K | 10-Q | 10-K/A | 8-K ...
    filed      DATE    NOT NULL,
    frame      TEXT               -- set only on SEC's canonical point (§5.4)
);

CREATE INDEX IF NOT EXISTS xbrl_facts_lookup_idx
    ON xbrl_facts (cik, taxonomy, tag, unit, end_date);

-- Dedup view: the same (period, concept) is re-reported by amendments and
-- later filings' comparative periods, values can differ (§5.3) and splits are
-- not restated (§5.9) — always take the LATEST-filed record per period.
CREATE OR REPLACE VIEW facts_dedup AS
SELECT DISTINCT ON (cik, taxonomy, tag, unit, start_date, end_date)
    *
FROM xbrl_facts
ORDER BY cik, taxonomy, tag, unit, start_date, end_date, filed DESC, accn DESC;

-- Canonical view: only SEC's official representative points (frames, §5.4).
CREATE OR REPLACE VIEW facts_canonical AS
SELECT * FROM xbrl_facts WHERE frame IS NOT NULL;

-- Metric -> tag priority list: one metric maps to several tags over time
-- (AAPL revenue used 3 different tags, §5.1). Seeded from app/xbrl.py.
CREATE TABLE IF NOT EXISTS metric_map (
    metric   TEXT NOT NULL,
    taxonomy TEXT NOT NULL,
    tag      TEXT NOT NULL,
    priority INT  NOT NULL,        -- 1 = preferred
    PRIMARY KEY (metric, taxonomy, tag)
);

-- Filing primary documents: accession -> the filing's main HTML document,
-- so numeric provenance can deep-link the SEC iXBRL viewer (the actual
-- 10-K text with every fact clickable) instead of the bare filing index.
-- Populated from the EDGAR submissions API at fact-ingestion time.
CREATE TABLE IF NOT EXISTS filing_docs (
    cik         BIGINT NOT NULL,
    accn        TEXT   NOT NULL,
    primary_doc TEXT   NOT NULL,
    PRIMARY KEY (cik, accn)
);
