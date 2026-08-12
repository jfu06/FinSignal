# FinSignal — Design Doc 

## 0. Relationship to decisions.md

decisions.md is the running log of target-architecture decisions; this design doc is the Phase-1 (one-day) implementation plan for the narrative/RAG line of that architecture. Where this doc deviates from a recorded decision, the deviation is deliberate and listed here:

| decisions.md decision | Phase 1 status |
|---|---|
| Narrative chunks + embeddings in Neon (pgvector) | ✅ Implemented as decided |
| Claim check as one batch verification call (not per-claim) | ✅ Implemented as decided |
| Question routing: numeric → SQL/metrics layer, narrative → RAG | ⏸ Deferred to Phase 2 — all questions go through RAG this phase; numeric/aggregation questions get an explicit "not yet supported" data-boundary reply |
| Numeric data from SEC XBRL Company Facts API | ⏸ Deferred to Phase 2 (schema and pitfalls already documented in data-dictionary.md) |
| Offline eval gate in CI on every model/prompt change | 🔽 Downgraded: the eval script has release-gate semantics (non-zero exit above threshold) but is run manually this phase |
| Golden set of 30–50 cases | 🔽 Downgraded: 10–15 cases this phase, same format, grows toward the target |
| Upload-document trust rules & sanitization | ⏸ Deferred to Phase 2 (fixed local corpus this phase) |

## 1. Goal & Scope

Build a small end-to-end demo: a user asks a natural-language question about a fixed set of pre-downloaded 10-K filings (2-3 well-known tickers), and gets back an answer with source citations and a per-claim credibility score.

This scope is intentionally small so it can be built and demoed in one focused day. Explicitly out of scope for this phase:

- Live/automated document crawling across the whole market
- Multi-market, multi-language support
- Production monitoring dashboards and alerting
- CI/CD wiring for the eval gate (the eval script itself is CI-ready, see Section 5)
- A separately trained/fine-tuned NLI model (use LLM-as-judge only)
- Numeric/XBRL metrics layer and question routing (Phase 2 — see the routing decision in decisions.md and docs/data-dictionary.md)
- User document upload and content-safety checks (Phase 2)

## 2. Data Source

- Manually download 2-3 real 10-K filings from SEC EDGAR (official, free, public source, no scraping, no login, no API key needed for basic full-text search/download).
- Pick well-known tickers so the content is easy to sanity-check yourself, e.g. AAPL, MSFT, TSLA.
- Store the downloaded filings as local files (e.g. data/raw/AAPL_10K.txt). This is a fixed, reproducible corpus, not a live search, which is what makes it possible to build a small eval set against it.

## 3. Architecture (Simplified)

The pipeline is intentionally linear, step by step:

1. Fixed 10-K files, downloaded manually and stored locally
2. Chunking: split each filing by paragraph/section
3. Embed all chunks once, store text + embeddings in a Neon Postgres table (pgvector)
4. At query time, retrieve the top-k chunks for the user's question from Neon with one pgvector cosine-similarity SQL query
5. LLM generates an answer as structured JSON, with each claim listing its cited chunk ids
6. One batch verification call (LLM-as-judge) checks all claims at once against their cited chunks, labeling each SUPPORTED, CONTRADICTED, or NOT_ENOUGH_INFO — one LLM call per report, not one per claim
7. Response assembler combines the answer, citations, per-claim credibility, and the overall unsupported rate, mapping NOT_ENOUGH_INFO → WARNING (shown, marked "unverified") and CONTRADICTED → ERROR (blocked, triggers regeneration) per the credibility rules in requirements.md
8. Structured logging records each step above (see Section 6)

## 4. Core Components

- Ingestion & Chunking: a simple script that splits each filing into paragraph/section-level chunks and stores chunk_id, doc_id, text, and section for each one.
- Retrieval: embed all chunks once (OpenAI embeddings or a local sentence-transformers model), store them in a Neon Postgres `chunks` table with a pgvector `embedding` column; query-time top-k is a single `ORDER BY embedding <=> :question_embedding LIMIT k` SQL query. This matches the storage decision in decisions.md and persists embeddings across runs (no re-embedding on every restart); Pinecone/Weaviate remain unnecessary at this scale.
- Answer Generation: prompt the model to return structured JSON with claims and cited chunk ids instead of free text, for example a claims array where each claim has a text field and a cited_chunk_ids field.
- Citation-Fidelity Scoring, one batch verification call (per decisions.md): after generation, send ALL claims plus their cited chunk texts to the judge in a single call, and get back a verdict plus a one-line reason for each claim — one LLM call per report instead of one per claim, cheaper and faster. No separate NLI model needed for this scope.
- Aggregation: the unsupported rate for a report equals the count of NOT_ENOUGH_INFO and CONTRADICTED claims divided by the total number of claims.

## 5. Evaluation (Mini Golden Set, No Existing Dataset Needed)

- Build 10-15 QA pairs manually (the Phase-1 subset of the 30–50-case golden-set target in decisions.md) instead of using a large labeled dataset. First use an LLM to draft candidate questions from the downloaded 10-Ks, then personally verify the correct answer and source paragraph for each one, roughly 1-2 hours of work.
- Store these as a simple JSON or CSV file with columns for question, ticker, expected_answer_snippet, and expected_chunk_id.
- Eval script: run the full pipeline on each test case, record the judge's verdict distribution, and report the overall unsupported rate plus a couple of concrete failure examples.
- The eval script doubles as the release gate: it exits non-zero when the unsupported rate exceeds the 4% threshold from project-requirements.md, so it is CI-ready as-is. At this scope, run it manually and include the output in the writeup/demo; wiring it into GitHub Actions is a deferred ~30-minute task.

## 6. Structured Logging

Structured logging is low effort at this scope, no dedicated logging infrastructure needed. Use Python's built-in logging module with a JSON formatter, or simply append one JSON object per event to a local .jsonl file. Log one record per key step, for example:

```
{"event": "query_received", "query_id": "q123", "ticker": "AAPL", "question": "..."}
{"event": "retrieval", "query_id": "q123", "retrieved_chunk_ids": ["c1", "c2"]}
{"event": "claim_generated", "query_id": "q123", "claim_id": "c1", "cited_chunk_ids": ["c1"]}
{"event": "claim_judged", "query_id": "q123", "claim_id": "c1", "verdict": "SUPPORTED"}
{"event": "report_summary", "query_id": "q123", "unsupported_rate": 0.1}
```

Why it matters even at this small scale: it lets you compute and chart the unsupported rate across your mini eval set by loading the .jsonl file into pandas and grouping by verdict, no dashboard needed. Effort estimate: about 30 minutes, since you only need to wrap the generation/judge calls with a small log_event helper that appends one JSON line per call.

## 7. Data Model (Simplified)

| Table | Key Fields |
|---|---|
| Chunk (Neon, pgvector) | chunk_id, doc_id, ticker, section, text, embedding |
| Claim | claim_id, query_id, text, cited_chunk_ids, verdict, judge_reason |
| TestCase | case_id, ticker, question, expected_answer_snippet, expected_chunk_id |

Phase 2 adds `xbrl_facts`, `metric_map`, and a QA-log table in the same Neon database — schemas already specified in docs/data-dictionary.md (Section 6).

## 8. What This Demonstrates

- A working end-to-end RAG pipeline with citation-grounded generation
- A concrete, quantifiable approach to hallucination detection, using LLM-as-judge scoring instead of just trusting the model
- Structured logging as the foundation for future evaluation/monitoring, without over-building infrastructure this project doesn't need yet
- A clear, honest scoping decision about what's in for a one-day MVP and what's explicitly deferred, which is itself worth explaining in an interview

## 9. Possible Next Steps (Not Built Now)

- Expand the Golden Test Set with more tickers and document types
- Add a small NLI model alongside the LLM-judge for cross-validation
- Move from local files to an automated EDGAR pull for a wider ticker list
- Build the numeric line: load SEC companyfacts into `xbrl_facts` (Neon), add `metric_map`, then implement question routing (numeric/aggregation → SQL metrics layer, narrative/explanatory → RAG, hybrid → merge) per decisions.md and docs/data-dictionary.md
- Add user document upload with the content-safety and trust rules from decisions.md
