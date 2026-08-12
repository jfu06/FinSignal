# FinSignal — Design Doc

## 1. Background & Goals

FinSignal is an AI investment research assistant for retail investors. A user asks a natural-language question about a Ticker or a financial document (10-K/10-Q/8-K/earnings call transcript/news), and the system returns an analysis with source citations and a credibility score for every claim.

The core engineering goal, distinct from similar products on the market (e.g. MoneySense.ai): not just "fast and readable", but every sentence must be verifiable. This doc focuses on two core subsystems:

- Citation-Fidelity Scoring (credibility quantification engine)
- Evaluation Harness (automated evaluation system)

### Goals
- Every claim in an answer can be traced back to a source location
- The unsupported-claim rate is quantified and continuously monitored, target < 4%
- Every model/prompt iteration is automatically evaluated before it can ship

### Non-Goals
- No real-time trade execution
- No personalized investment advice (information organization + credibility labeling only, with disclaimer)
- No multi-language/multi-market support in Phase 1, nail the single US-equities scenario first

## 2. System Architecture

```
User question
   |
      v
      [API Gateway]
         |
            v
            [Document Ingestion & Index Service]  <-- pulls SEC filings, earnings, news on schedule/on demand
               |
                  v
                  [Retrieval Layer (RAG)]
                     |
                        v
                        [Answer Generation Agent] --calls--> [Claim Segmentation]
                           |                                       |
                              |                                       v
                                 |                          [Citation-Fidelity Scoring Engine]
                                    |                                       |
                                       v                                       v
                                       [Response Assembler] <---------- scores + citation locations
                                          |
                                             v
                                             User-facing display (claims + citations + credibility scores + risk flags)

                                             (side path)
                                             [Evaluation Harness] --triggered on each release/on schedule--> compare against Golden Test Set --> report --> gate release
                                             ```

                                             ## 3. Core Components

                                             ### 3.1 Document Ingestion & Index Service
                                             - Input: Ticker/document source (PDF, URL, SEC filing pulled via API)
                                             - Processing: parse -> chunk along semantic boundaries (recommended 300-500 tokens/chunk, keep section headers as metadata) -> embed -> write to vector store
                                             - Each chunk stores: `chunk_id`, `doc_id`, `page_no`, `section`, `text`, `embedding`
                                             - This layer guarantees later citations can point to an exact page/paragraph, which is the precondition for citation-fidelity to work

                                             ### 3.2 Retrieval Layer (RAG)
                                             - User question -> embedding -> vector search top-k chunks (recommended k=8-15, then rerank down to 4-6)
                                             - Output: candidate chunk list with similarity scores

                                             ### 3.3 Answer Generation Agent
                                             - Require the model to output structured JSON instead of free text, e.g.:
                                             ```json
                                             {
                                               "claims": [
                                                   {
                                                         "claim_id": "c1",
                                                               "text": "Apple's FY2024 services revenue grew 14% YoY",
                                                                     "cited_chunk_ids": ["doc123_chunk045"]
                                                                         }
                                                                           ]
                                                                           }
                                                                           ```
                                                                           - Key design point: require the model to self-report citations at generation time, rather than reconstructing them afterward, this is what makes fidelity scoring possible downstream

                                                                           ### 3.4 Claim Segmentation (fallback for non-conforming output)
                                                                           - If the model still returns free text, use a lightweight LLM call to split the answer into atomic claims, each an independently verifiable factual statement
                                                                           - Purpose: avoid a whole paragraph mapping to one vague citation, which would make credibility scoring meaningless

                                                                           ## 4. Citation-Fidelity Calculation (Core)

                                                                           This is the product's core differentiator, broken into four steps:

                                                                           **Step 1: Claim Decomposition**
                                                                           Split the generated answer into a set of atomic claims `{claim_1, claim_2, ...}`, each independently verifiable.

                                                                           **Step 2: Evidence Retrieval**
                                                                           For each claim, take the model's self-reported `cited_chunk_ids`; if none were given, re-run a vector search using the claim text and take the most relevant chunk as candidate evidence (flagged as "weak citation", scored lower later).

                                                                           **Step 3: Entailment Check**
                                                                           Use an independent judge model (a small NLI model, an LLM-as-judge, or ideally both cross-validated) to classify the relationship between claim and chunk into three categories:
                                                                           - `SUPPORTED`: the chunk entails the claim
                                                                           - `CONTRADICTED`: the chunk contradicts the claim
                                                                           - `NOT_ENOUGH_INFO`: the chunk neither supports nor contradicts the claim

                                                                           **Step 4: Score Aggregation**
                                                                           - Per-claim credibility score:
                                                                             - `SUPPORTED` -> 0.7-1.0 (graded by the judge model's confidence)
                                                                               - `NOT_ENOUGH_INFO` -> 0.2-0.5, flagged as unsupported
                                                                                 - `CONTRADICTED` -> 0 (triggers the blocking rule from the requirements doc)
                                                                                 - Report-level unsupported claim rate:
                                                                                   ```
                                                                                     unsupported_rate = (count(NOT_ENOUGH_INFO) + count(CONTRADICTED)) / total_claims
                                                                                       ```
                                                                                       - This unsupported_rate is used both for the pre-display gating decision on a single generation and rolled up into the Evaluation Harness's historical trend metrics

                                                                                       ## 5. Evaluation Harness Design

                                                                                       ### 5.1 Golden Test Set
                                                                                       - Manually built by the content team: a set of `(document, question, expected_claims, expected_citations)` samples
                                                                                       - Recommended coverage: normal Q&A, ambiguous questions, questions where the document genuinely has no answer (to test whether the model fabricates one), and compound questions requiring multiple chunks
                                                                                       - Prioritize high-frequency question types first (revenue drivers, risk factors, YoY changes), then expand

                                                                                       ### 5.2 Automated Evaluation Pipeline
                                                                                       - Trigger: automatically on every model/prompt/retrieval-strategy change (wired into CI/CD), plus a daily scheduled run for trend monitoring
                                                                                       - Flow: pull Golden Test Set -> run the full pipeline (retrieval + generation + scoring) for each case -> compare against human-labeled expected results -> output a metrics report
                                                                                       - Core metrics:
                                                                                         - Unsupported claim rate (target < 4%)
                                                                                           - Citation localization accuracy (does the model's cited chunk match the human-labeled correct chunk)
                                                                                             - Contradiction rate (should be 0; any occurrence is treated as a serious bug)
                                                                                               - End-to-end latency (P50/P95)

                                                                                               ### 5.3 Release Gating
                                                                                               - If unsupported claim rate or contradiction rate exceeds threshold, the CI pipeline automatically fails and blocks release
                                                                                               - Manual override is allowed but must record a reason (for audit purposes)

                                                                                               ### 5.4 Continuous Production Monitoring
                                                                                               - Sample real production traffic (e.g. 5%), run fidelity scoring asynchronously without blocking the user response
                                                                                               - Results feed a monitoring dashboard tracking unsupported rate daily/weekly, with alerts on threshold breaches
                                                                                               - Edge cases (spikes in NOT_ENOUGH_INFO, anomalies for a specific document type) go into a human review queue, and the content team adds them back into the Golden Test Set, closing the loop

                                                                                               ## 6. Data Model (Simplified)

                                                                                               | Table | Key Fields |
                                                                                               |---|---|
                                                                                               | Document | doc_id, ticker, doc_type, source_url, ingested_at |
                                                                                               | Chunk | chunk_id, doc_id, page_no, section, text, embedding |
                                                                                               | Query | query_id, user_id, ticker, question, created_at |
                                                                                               | Claim | claim_id, query_id, text, cited_chunk_ids, verdict(SUPPORTED/CONTRADICTED/NOT_ENOUGH_INFO), score |
                                                                                               | EvaluationRun | run_id, model_version, prompt_version, unsupported_rate, contradiction_rate, passed(bool), created_at |
                                                                                               | GoldenTestCase | case_id, doc_id, question, expected_claims, expected_citations |

                                                                                               ## 7. Non-Functional Requirements

                                                                                               - **Latency**: end-to-end target < 8s per question (retrieval + generation + scoring)
                                                                                               - **Scalability**: vector store and chunk storage must support millions of chunks (covering historical filings across many tickers)
                                                                                               - **Compliance**: every answer page must display a disclaimer; user investment-decision behavior must not be stored as identifiable PII
                                                                                               - **Observability**: every Evaluation Harness run must be traceable (model version, prompt version, dataset version)

                                                                                               ## 8. Phased Plan

                                                                                               - **Phase 1**: US equities only, English documents, 10-K/10-Q first, close the Citation-Fidelity + Evaluation Harness loop
                                                                                               - **Phase 2**: extend to earnings call transcripts and news; add multi-language/multi-market support
                                                                                               - **Phase 3**: historical signal review, subscriptions, personalized summaries

                                                                                               ## 9. Open Questions

                                                                                               - NLI judge model choice: small model (cheap, can run offline) vs. LLM-as-judge (more accurate, more expensive)? Recommend running both in parallel in Phase 1 for cross-validation before deciding long-term
                                                                                               - Who owns ongoing maintenance of the Golden Test Set, and at what cadence, needs confirmation with the customer
                                                                                               - Should the unsupported-claim-rate threshold (4%) be a single global threshold, or vary by document type (10-K vs. news)?
                                                                                               
