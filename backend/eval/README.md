# Evaluation (mini golden set + release gate)

Release-gate evaluation per design-doc §5.

- [`golden_set.json`](golden_set.json) — 30 manually verified cases:
  25 narrative QA pairs (each anchored to a content-verified
  `expected_chunk_id` in the ingested corpus, spanning business, risk,
  MD&A, market/dividend, sustainability, and key-person topics across
  AAPL/MSFT/TSLA, in English and Chinese) + 5 numeric-boundary cases that
  must receive the "not yet supported" reply. Grows toward the 30–50 target.
- [`run_eval.py`](run_eval.py) — runs the FULL pipeline on every case,
  syncs cases into the `test_cases` table, reports the judge verdict
  distribution / retrieval hit rate / **span-overlap second signal**
  (deterministic number-match + English token overlap beside the LLM judge)
  / failure examples, and **exits non-zero when the unsupported rate exceeds
  the 4% threshold** (`UNSUPPORTED_RATE_THRESHOLD`). Crashed cases are
  retried once; unresolved crashes also fail the gate. CI-ready as-is; run
  manually this phase:

```bash
cd backend && python -m eval.run_eval
```

Latest run (30 cases): **0.00% unsupported rate (0/151 claims), 25/25
retrieval hits, 5/5 boundary cases OK, number-match avg 0.97 — GATE PASSED.**

## Evaluation layers

1. **Human-labeled golden set** (this directory) — the release-gate
   regression; anchored to documents an analyst has read. Covers the demo
   trio; grows by labeling, never "automatically covers everything".
2. **Self-supervised smoke eval** (`app/smoke_eval.py`,
   `python -m app.smoke_eval <TICKER>`) — for on-demand onboarded tickers:
   questions are generated FROM sampled chunks, so each question's source
   chunk is ground truth for free. Certifies retrieval-findability and
   zero-unsupported-claims on that document; does NOT certify human-judged
   answer quality. Results persist to `data/raw/{TICKER}_smoke.json` and
   surface in the UI.
3. **Runtime guardrails** (judge + span-overlap, every query) — the only
   layer that inherently covers every company.

Notes:
- Span-overlap flags are advisory review prompts, not blocks. Known flag
  sources: model-derived/rounded figures (correct but not verbatim in the
  citation) and cross-unit round numbers (``200亿`` vs ``$20 billion`` —
  digit-sequence matching can't bridge those).
- Chunk ids shift when chunking rules change — after any re-ingest that
  changes chunk counts, re-verify `expected_chunk_id` anchors by content.
