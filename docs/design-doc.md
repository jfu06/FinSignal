# FinSignal — Design Doc 

## 1. Goal & Scope

Build a small end-to-end demo: a user asks a natural-language question about a fixed set of pre-downloaded 10-K filings (2-3 well-known tickers), and gets back an answer with source citations and a per-claim credibility score.

This scope is intentionally small so it can be built and demoed in 1-2 days. Explicitly out of scope for this phase:

- Live/automated document crawling across the whole market
- Multi-market, multi-language support
- Production monitoring dashboards and alerting
- CI/CD release gating on every model/prompt change
- A separately trained/fine-tuned NLI model (use LLM-as-judge only)
- Vector database infrastructure (in-memory search is enough at this scale)

## 2. Data Source

- Manually download 2-3 real 10-K filings from SEC EDGAR (official, free, public source, no scraping, no login, no API key needed for basic full-text search/download).
- Pick well-known tickers so the content is easy to sanity-check yourself, e.g. AAPL, MSFT, TSLA.
- Store the downloaded filings as local files (e.g. data/raw/AAPL_10K.txt). This is a fixed, reproducible corpus, not a live search, which is what makes it possible to build a small eval set against it.

## 3. Architecture (Simplified)

The pipeline is intentionally linear, step by step:

1. Fixed 10-K files, downloaded manually and stored locally
2. Chunking: split each filing by paragraph/section
3. Embed all chunks once, then use in-memory cosine similarity search (no vector DB needed)
4. At query time, retrieve the top-k chunks for the user's question
5. LLM generates an answer as structured JSON, with each claim listing its cited chunk ids
6. LLM-as-judge scores each claim as SUPPORTED, CONTRADICTED, or NOT_ENOUGH_INFO
7. Response assembler combines the answer, citations, per-claim credibility, and the overall unsupported rate
8. Structured logging records each step above (see Section 6)

## 4. Core Components

- Ingestion & Chunking: a simple script that splits each filing into paragraph/section-level chunks and stores chunk_id, doc_id, text, and section for each one.
- Retrieval: embed all chunks once (OpenAI embeddings or a local sentence-transformers model), then do cosine similarity search at query time. A Python list/array is enough, no need for Pinecone or Weaviate at this scale.
- Answer Generation: prompt the model to return structured JSON with claims and cited chunk ids instead of free text, for example a claims array where each claim has a text field and a cited_chunk_ids field.
- Citation-Fidelity Scoring, LLM-as-judge only: for each claim, send the claim text and its cited chunk text to a judge prompt, and get back a verdict plus a one-line reason. No separate NLI model needed for this scope.
- Aggregation: the unsupported rate for a report equals the count of NOT_ENOUGH_INFO and CONTRADICTED claims divided by the total number of claims.

## 5. Evaluation (Mini Golden Set, No Existing Dataset Needed)

- Build 10-15 QA pairs manually instead of using a large labeled dataset. First use an LLM to draft candidate questions from the downloaded 10-Ks, then personally verify the correct answer and source paragraph for each one, roughly 1-2 hours of work.
- Store these as a simple JSON or CSV file with columns for question, ticker, expected_answer_snippet, and expected_chunk_id.
- Eval script: run the full pipeline on each test case, record the judge's verdict distribution, and report the overall unsupported rate plus a couple of concrete failure examples.
- No CI/CD gating needed at this scope, just run the eval script manually and include the output or screenshots in your writeup or demo.

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
| Chunk | chunk_id, doc_id, ticker, section, text |
| Claim | claim_id, query_id, text, cited_chunk_ids, verdict, judge_reason |
| TestCase | case_id, ticker, question, expected_answer_snippet, expected_chunk_id |

## 8. What This Demonstrates

- A working end-to-end RAG pipeline with citation-grounded generation
- A concrete, quantifiable approach to hallucination detection, using LLM-as-judge scoring instead of just trusting the model
- Structured logging as the foundation for future evaluation/monitoring, without over-building infrastructure this project doesn't need yet
- A clear, honest scoping decision about what's in for a 1-2 day MVP and what's explicitly deferred, which is itself worth explaining in an interview

## 9. Possible Next Steps (Not Built Now)

- Expand the Golden Test Set with more tickers and document types
- Add a small NLI model alongside the LLM-judge for cross-validation
- Move from local files to an automated EDGAR pull for a wider ticker list
