# FinSignal — Phase 1 (RAG line)

A verifiable, citation-grounded Q&A assistant over a **fixed local set of 10-K
filings**. A retail investor asks a natural-language question about a ticker and
gets back a plain-language answer, per-claim source citations, and a per-claim
credibility label — with unsupported claims flagged and contradicted claims
blocked.

This repo implements **only the narrative/RAG line** described in
[`docs/design-doc.md`](docs/design-doc.md). See Section 0 of that doc for the
authoritative scope table.

### Out of scope this phase (do not build)

Numeric/XBRL metrics layer · question routing (numeric vs. narrative) ·
document upload · CI wiring of the eval gate · a trained NLI model ·
monitoring dashboards · multi-market support.

> Numeric / aggregation questions are **not** answered by RAG — the pipeline
> returns an explicit "not yet supported" data-boundary reply (routing is
> Phase 2).

---

## Pipeline

```
local 10-K files
  → chunk (paragraph/section, XBRL-noise filtered)
  → embed once, store text + embedding in Neon (pgvector)
  → [query] top-k cosine retrieval  (single pgvector SQL query)
      ↺ bounded agentic refinement: an LLM assessor judges sufficiency and
        picks ENOUGH / REWRITE query / EXPAND k / GIVE_UP (≤3 rounds, k≤24,
        every decision logged; invalid output fails safe to one-shot behavior)
  → LLM answer as structured JSON (claims + cited_chunk_ids)
  → ONE batch verification call (LLM-as-judge over ALL claims at once)
  → assemble report + credibility mapping + unsupported rate
  → structured jsonl logging at every step
```

### Credibility mapping (from `docs/requirements.md`)

| Judge verdict     | Status  | Behavior                                                        |
|-------------------|---------|----------------------------------------------------------------|
| `SUPPORTED`       | OK      | Shown normally with a clickable citation                       |
| `NOT_ENOUGH_INFO` | WARNING | Shown but marked **unverified**; added to the unsupported list |
| `CONTRADICTED`    | ERROR   | **Blocked** (not shown); logged and triggers one regeneration  |

The **unsupported rate** = `(NOT_ENOUGH_INFO + CONTRADICTED) / total claims`.
The eval script is a release gate: it **exits non-zero when the unsupported
rate exceeds 4%** (`UNSUPPORTED_RATE_THRESHOLD`).

---

## Project layout

```
backend/
  app/
    config.py         # env/.env config (DATABASE_URL, keys, models, thresholds)
    logging_utils.py  # log_event(): one jsonl line per step (design-doc §6)
    models.py         # Chunk / Claim / TestCase + Verdict→Status mapping (§7)
    db.py             # Neon connection (pgvector) + schema init
    schema.sql        # DDL: chunks(+vector) / claims / test_cases
    embeddings.py     # local e5 embeddings (query:/passage: prefixes)
    ingest.py         # chunk + embed + store
    retrieval.py      # single pgvector top-k query
    refinement.py     # bounded agentic retrieval-refinement loop
    schemas.py        # Pydantic models for every LLM tool output (lenient
                      #   salvage validators for observed malformed shapes)
    generation.py     # structured JSON claim generation (forced tool call)
    verification.py   # ONE batch judge call per report
    assembler.py      # report + credibility mapping
    pipeline.py       # LangGraph StateGraph orchestration (regen loop = conditional edge)
    edgar.py          # SEC EDGAR client (any US ticker via official mapping)
    onboarding.py     # on-demand ticker onboarding (download → ingest → ready)
    smoke_eval.py     # self-supervised smoke eval for onboarded tickers
                      #   (synthetic QA from sampled chunks — no human labels)
  ui/app.py           # Streamlit demo UI (citations, WARNING ack, export,
                      #   add-any-company via EDGAR)
  eval/               # golden set + release-gate script
  tests/              # unit tests
  data/raw/           # downloaded 10-K files (not committed)
  requirements.txt
  .env.example
docs/                 # design-doc, requirements, decisions, data-dictionary
```

---

## Setup

### 1. Python environment

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure secrets

```bash
cp .env.example .env
# edit .env: set DATABASE_URL, ANTHROPIC_API_KEY
```

`.env` is git-ignored. `DATABASE_URL` is read from the environment; the LLM
key is read from `.env`. **No secret is ever hard-coded or committed.**

- **LLM (generation + batch judge):** Anthropic Claude (`LLM_MODEL`).
- **Embeddings:** run **locally** via sentence-transformers
  (`intfloat/multilingual-e5-small`, 384-dim) — no API key needed; this is the
  local-model option from design-doc §4. **Multilingual matters**: questions
  may be Chinese while the 10-K corpus is English (see the sample question in
  requirements.md). e5 requires **both-side prefixes** (`"query: "` /
  `"passage: "`); `app/embeddings.py` applies them automatically.

> `EMBEDDING_DIM` in `.env` must match the embedding model **and** the
> `vector(N)` column in `schema.sql`. Default: `multilingual-e5-small` → 384.
> Changing the embedding model requires re-running `python -m app.ingest`.

### 3. Provision Neon (pgvector)

Create a free Postgres database at [neon.tech](https://neon.tech), enable
pgvector, and create the tables. You can apply the schema in one command:

```bash
python -m app.db          # runs backend/app/schema.sql against DATABASE_URL
```

…or paste the SQL below into the Neon SQL editor:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- 1) Narrative corpus + embeddings.
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   TEXT PRIMARY KEY,          -- e.g. "AAPL_10K_0001"
    doc_id     TEXT NOT NULL,             -- e.g. "AAPL_10K_2023"
    ticker     TEXT NOT NULL,             -- e.g. "AAPL"
    section    TEXT,                       -- filing section/heading, may be NULL
    text       TEXT NOT NULL,
    embedding  vector(384) NOT NULL,       -- must match EMBEDDING_DIM
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chunks_ticker_idx ON chunks (ticker);
CREATE INDEX IF NOT EXISTS chunks_embedding_cosine_idx
    ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- 2) Generated claims + judge verdicts.
CREATE TABLE IF NOT EXISTS claims (
    claim_id        TEXT PRIMARY KEY,
    query_id        TEXT NOT NULL,
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

-- 3) Mini golden set for the eval / release gate.
CREATE TABLE IF NOT EXISTS test_cases (
    case_id                 TEXT PRIMARY KEY,
    ticker                  TEXT NOT NULL,
    question                TEXT NOT NULL,
    expected_answer_snippet TEXT,
    expected_chunk_id       TEXT
);
```

The canonical DDL lives in [`backend/app/schema.sql`](backend/app/schema.sql);
the block above is a copy for convenience.

---

## Deploying publicly

All config is env-driven — set `DATABASE_URL` and `ANTHROPIC_API_KEY` as
platform secrets, no code changes. Two supported paths:

- **Streamlit Community Cloud** (free): connect the GitHub repo, main file
  `backend/ui/app.py`, Python 3.11 (`runtime.txt`), paste secrets in the app
  settings. Root `requirements.txt` pulls in `backend/requirements.txt` with
  CPU torch wheels.
- **Any container host** (HF Spaces PRO / Railway / Fly / Render): the root
  `Dockerfile` serves the UI on port 7860.

**Before exposing to the internet, activate the cost guardrails** (inactive by
default for local dev):

| Env var | Effect |
|---|---|
| `ACCESS_CODE` | Non-empty → UI requires this code before use |
| `SESSION_QUERY_LIMIT` | Max questions per browser session (default 10) |
| `DAILY_QUERY_BUDGET` | Max questions per UTC day across all users (default 50) |
| `MAX_TICKERS` | Corpus cap for on-demand onboarding (default 10, enforced server-side) |

Also recommended: set a monthly spend limit on the Anthropic key in their
console (hard backstop), and rotate any credentials before going live.

## Tests

```bash
cd backend
pytest
```

---

## Usage

```bash
cd backend && source .venv/bin/activate

# one-time setup
python -m app.db                        # apply schema to Neon
python -m scripts.download_filings      # fetch AAPL/MSFT/TSLA 10-Ks from EDGAR
python -m app.ingest                    # chunk + embed + store (766 chunks)

# ask a question (add a path as 3rd arg to export the report as JSON)
python -m app.pipeline "苹果最近一年的营收增长主要靠什么驱动？" AAPL report.json

# web UI (click-through citations, WARNING acknowledgment, JSON export)
streamlit run ui/app.py

# release-gate evaluation (exits non-zero if unsupported rate > 4%)
python -m eval.run_eval
```

## Implementation status

- [x] **Step 1** — project skeleton, `.env.example`, README + Neon schema SQL
- [x] **Step 2** — corpus download + ingestion/chunking + local embeddings
- [x] **Step 3** — retrieval (single pgvector top-k query, cross-lingual e5)
- [x] **Step 4** — answer generation (forced-tool structured JSON claims)
- [x] **Step 5** — batch verification (ONE judge call) + credibility assembly
      + one-shot regeneration on CONTRADICTED
- [x] **Step 6** — golden set (30 cases) + eval release gate
      (latest run: 0.00% unsupported rate, 25/25 retrieval hits — GATE PASSED)
- [x] **Post-MVP** — bounded agentic retrieval-refinement loop · LangGraph
      StateGraph port · span-overlap second signal (number-match + token
      overlap) · full LLM-call tracing (prompt + raw output per call, keyed
      by query_id)
