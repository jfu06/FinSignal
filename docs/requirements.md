# FinSignal — Customer Requirements

## Customer

A retail-facing fintech startup building an AI investment research assistant for individual investors.

## Use Case

Retail investors should be able to ask natural-language questions about a stock or financial document (10-K, 10-Q, earnings call transcript, news article) and get instant, trustworthy analysis with verifiable citations — without reading hundreds of pages themselves.

## Why This Is Urgent

Retail investors are overwhelmed by long, dense financial filings (some 300+ pages). Existing AI research tools already summarize these documents fast, but they don't tell users which parts of the answer are actually grounded in the source document versus made up by the model. Users can't tell if they're making decisions based on real facts or hallucinated ones — this is a trust and safety gap, not just a speed gap.

## What We Do Today (Baseline Capability)

A user picks a ticker or uploads a document, and the AI generates an instant analysis. Inputs:

- Ticker / Company Name, string
- Document Type (10-K / 10-Q / 8-K / Earnings Call Transcript / News Article), enum
- Document Source, PDF or URL
- Market Region, enum (covering multiple global markets)
- User Question, natural language string
- User Risk Profile (optional), enum (Conservative/Moderate/Aggressive)

## What We Need The Tool To Do

- Allow a retail investor to select a ticker or upload a document
- Parse and index long documents (up to hundreds of pages) for retrieval
- Answer the user's question in plain language with inline citations back to the exact source paragraph
- Quantify a credibility/confidence score for every claim generated (not just for the whole report)
- Detect and flag claims that are not supported by the source document (unsupported/hallucinated claims)
- Run an automated evaluation harness that continuously measures the unsupported-claim rate against a labeled test set, and blocks or downgrades outputs that exceed a set threshold
- Let the user jump directly from any claim to the underlying source text
- Export the analysis report for personal reference

## Production-Ready Requirements

- Every input is validated
- Integrity rules always enforce consistency
- Errors are safe, clear, and contained
- Code is modular and navigable
- Critical logic (especially citation matching and scoring) is covered by automated tests
- Evaluation harness runs automatically on every model/prompt change, not just manually
- Project runs end-to-end out of the box

## Sample Data

- Ticker: AAPL
- Document Type: 10-K
- Market Region: US
- User Question: "苹果最近一年的营收增长主要靠什么驱动？有没有风险因素？"

## Clarified & Structured Requirements

### Users

The direct users are retail investors, whose goal is to better understand filings/announcements and make more informed investment decisions — the product does not make investment decisions for them and is not investment advice. Analysts / content team act behind the scenes, maintaining the evaluation dataset and reviewing evaluation results; they do not face retail users directly.

### Analysis Report

One Analysis Report corresponds to one user question + one Ticker/document combination. Output must include:

- Key Insight Summary (plain language)
- Cited Evidence (each claim with source citation and location)
- Credibility Score (per-claim, not just one score for the whole report)
- Risk Flags
- Unsupported Claims List (claims not backed by the source, called out explicitly)
- Disclaimer (for reference only, not investment advice)

### Credibility Handling Rules

| Scenario | Handling | Reason |
|---|---|---|
| Claim has a clear citation and the citation matches the source | ✅ Display normally with clickable source link | Verifiable, safe to show |
| Claim has no supporting evidence in the source (unsupported claim) | ⚠️ WARNING - marked "unverified", credibility score lowered, still shown but requires user acknowledgment | Transparent, but risk must not be hidden |
| Claim clearly contradicts or over-infers from the source | ❌ ERROR - block the claim, do not display, trigger regeneration or manual review | Prevent misleading the user |
| Same user asks the same question about the same Ticker/document again | ⚠️ WARNING - offer to reuse cached result or force regeneration | Avoid wasted compute while keeping content fresh |
| Evaluation Harness detects unsupported claim rate above threshold (e.g. 4%) for a batch | ❌ ERROR - batch is not shipped, triggers auto-retry or manual review | Protect the product's credibility baseline |

### Functional Requirements

| Feature | Required? | Notes |
|---|---|---|
| Long document parsing & retrieval (10-K/earnings/news, hundreds of pages) | ✅ Required | Core foundational capability |
| Natural language Q&A with per-claim citation | ✅ Required | Users can click through to the source, unlike plain summarizers |
| Citation-fidelity credibility scoring | ✅ Required | Core differentiator, scored per claim rather than per report |
| Evaluation Harness (automated evaluation system) | ✅ Required | Continuously monitors unsupported claim rate against labeled test set, gates releases |
| Unsupported/contradictory claim detection & blocking | ✅ Required | Handling rules as above |
| Multi-market coverage (US equities first, expand later) | Phase 2 | Nail the US equities scenario and evaluation loop first |
| Analysis report export | ✅ Required | For the user's personal reference |
| Historical question/signal review | Optional | Phase 2 |

### Open Questions To Confirm With Customer

- Does displaying financial analysis to retail users trigger any regulatory/compliance requirements (investment advice disclosures, licensing)?
- Who owns and maintains the Golden Test Set long-term, and how often is it refreshed?
- Should the unsupported-claim-rate threshold (4%) be uniform across all document types, or vary by type (e.g., 10-K vs. news)?
