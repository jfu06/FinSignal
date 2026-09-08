# SEC XBRL Numeric Data Dictionary

> Covers the free official SEC XBRL APIs (data.sec.gov) that FinSignal's
> numeric data layer is built on. Compiled from live API responses measured on
> 2026-08-12, sampled against Apple Inc. (CIK 0000320193, US-GAAP filer) and
> SAP SE (CIK 0001000184, IFRS filer). Every field name, unit, and date range
> below comes from actual returned values, not paraphrased documentation.

## 0. Conventions (all endpoints)

| Convention | Details |
|---|---|
| CIK format | The URL requires a **10-digit, zero-padded** CIK (`CIK0000320193`); the `cik` field in the returned JSON is a plain number without leading zeros (`320193`) |
| Access | No API key; a self-identifying `User-Agent` header is required; respect the SEC fair-access policy (~10 requests/sec) |
| Freshness | The three XBRL endpoints update within ~1 minute of a filing's acceptance; a full nightly bundle `companyfacts.zip` refreshes ~3am ET (use it for offline ingestion instead of crawling the API) |
| Value scale | `val` is in **raw units**: dollars are dollars (AAPL net income `3496000000`), not thousands/millions; per-share values are dollars per share |
| Coverage | Only **standard-taxonomy** tags (us-gaap / ifrs-full / dei / srt). Company-specific extension tags (e.g. `aapl:`-prefixed per-product revenue) appear in **no endpoint** — the only source for those is the original filing |
| History start | Mandatory XBRL filing phased in from 2009; large filers' data effectively starts with filings submitted in 2009 (whose comparative periods reach further back — AAPL's earliest data point is `end=2006-09-30`) |

## 1. Company Facts endpoint (the main table; FinSignal's ingestion entry)

`GET https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json` — every
standard-tag value for one company, in one response.

### 1.1 Top-level structure

| Field | Type | Notes |
|---|---|---|
| `cik` | number | No leading zeros |
| `entityName` | string | Company name (e.g. `Apple Inc.`) |
| `facts` | object | Two-level nesting: `facts[taxonomy][tag]` |

### 1.2 The two key levels of `facts`

| Level | Possible values | Measured |
|---|---|---|
| taxonomy | `dei`, `us-gaap`, `ifrs-full` (foreign issuers), `srt`, … | AAPL: `dei` (2 tags) + `us-gaap` (503 tags); SAP: `dei` (1) + `ifrs-full` (368). **The same concept has entirely different tags across the two taxonomies** |
| tag | Standard taxonomy concept name | e.g. `NetIncomeLoss`, `Assets` |

Each tag object carries three fields: `label` (human-readable name),
`description` (the standard's definition, long text), and `units` (data-point
arrays grouped by unit of measure).

### 1.3 `units` keys (units of measure)

Units are **string keys**; one tag can carry several unit arrays at once.
Observed in practice:

| Unit | Meaning | Measured example |
|---|---|---|
| `USD`, `EUR`, … (ISO currencies) | Monetary amounts | AAPL: 446 tags in `USD`; SAP: 345 tags in `EUR`, 91 in `USD` |
| `shares` | Share counts | Outstanding shares, weighted shares |
| `USD/shares`, `EUR/shares` | Per-share amounts | EPS, dividends per share |
| `pure` | Dimensionless (ratios, tax rates) | `EffectiveIncomeTaxRateContinuingOperations` |
| `Year` / `Y` | Year counts (durations/lives) | **Two spellings for the same meaning**: AAPL uses `Year`, SAP uses `Y` |
| `USD/EUR` etc. | Exchange rates | SAP has `AUD/EUR`, `JPY/EUR`, … |
| Custom units | Arbitrary strings | AAPL has `Store` (store counts); SAP has `employee`, `item` |

### 1.4 Data-point record structure (identical in companyconcept)

Each element of a `units` array is "one value as reported in one filing":

| Field | Type | Required | Meaning | Pitfall |
|---|---|---|---|---|
| `start` | date | no | Period start | **Instant-type concepts have no `start`** (see 5.5) |
| `end` | date | yes | Period end / point-in-time date | Filter by period using `start`/`end` |
| `val` | number | yes | Value, in raw units | — |
| `accn` | string | yes | Accession number — traces back to the exact filing | This is what citation provenance hangs on |
| `fy` | number | yes | **The fiscal year of the FILING**, not of the data | See 5.2 — the biggest trap |
| `fp` | string | yes | The filing's fiscal period: `FY`/`Q1`/`Q2`/`Q3`/`Q4` | Same trap as `fy` |
| `form` | string | yes | Form type: `10-K`, `10-Q`, `10-K/A`, `8-K`, … | `/A` is an amendment; its value may differ from the original |
| `filed` | date | yes | Submission date | The basis for "keep the latest" deduplication |
| `frame` | string | no | Calendar-period marker, e.g. `CY2007`, `CY2009Q1` | **Only present on the record the SEC picked as that period's "official representative point"** (see 5.4) |

## 2. Company Concept endpoint (single-metric detail)

`GET https://data.sec.gov/api/xbrl/companyconcept/CIK##########/{taxonomy}/{tag}.json`
— one company × one concept.

| Field | Type | Notes |
|---|---|---|
| `cik` | number | As above |
| `taxonomy` / `tag` | string | e.g. `us-gaap` / `EarningsPerShareDiluted` |
| `label` / `description` | string | Concept name and standard definition |
| `entityName` | string | Company name |
| `units` | object | Identical structure to 1.3 / 1.4 |

Use case: fetching a single metric on demand when companyfacts is too large;
the data-point structure is the same as the main table.

## 3. Frames endpoint (cross-section: one period × one metric × the market)

`GET https://data.sec.gov/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json`
— one record per company that reported the concept in a calendar period.

### 3.1 URL parameter conventions

| Param | Format | Example | Note |
|---|---|---|---|
| `unit` | Compound units joined with `-per-` | `USD`, `USD-per-shares` | **Inconsistent** with the `USD/shares` spelling inside data points |
| `period` | Annual `CY####` (365±30 days); quarterly `CY####Q#` (91±30 days); instant `CY####Q#I` | `CY2025Q4`, `CY2026Q1I` | Instant concepts require the `I` suffix |

### 3.2 Response structure

| Field | Type | Notes |
|---|---|---|
| `taxonomy` / `tag` / `uom` / `ccp` | string | `ccp` echoes the requested calendar period (e.g. `CY2026Q1I`) |
| `label` / `description` | string | Concept definition |
| `pts` | number | Data-point count = `data.length` |
| `data[]` | array | One row per company: `accn`, `cik`, `entityName`, `loc` (registrant location, e.g. `US-IL`), `start` (duration types only), `end`, `val` |

Measured: `Assets/USD/CY2026Q1I` returns 5,512 companies;
`Revenues/USD/CY2025Q4` only 378 (see 5.1 and 5.6 — most companies report
revenue under other tags, and standalone Q4 is rarely filed directly).

## 4. Core metric reference (AAPL measured: unit, type, time range)

| Metric | tag (us-gaap) | Unit | Type | Measured range (`end` dates) | Points |
|---|---|---|---|---|---|
| Revenue (post-ASC 606) | `RevenueFromContractWithCustomerExcludingAssessedTax` | USD | duration | 2017-09-30 – 2026-06-27 | 117 |
| Revenue (legacy tag, retired) | `SalesRevenueNet` | USD | duration | 2007-09-29 – 2018-06-30 | 210 |
| Revenue (generic tag, briefly used by AAPL) | `Revenues` | USD | duration | 2016-09-24 – 2018-09-29 | 11 |
| Cost of sales | `CostOfGoodsAndServicesSold` | USD | duration | 2007-09-29 – 2026-06-27 | 234 |
| Gross profit | `GrossProfit` | USD | duration | 2007-09-29 – 2026-06-27 | 338 |
| R&D expense | `ResearchAndDevelopmentExpense` | USD | duration | 2007-09-29 – 2026-06-27 | 234 |
| Operating income | `OperatingIncomeLoss` | USD | duration | 2007-09-29 – 2026-06-27 | 234 |
| Income tax expense | `IncomeTaxExpenseBenefit` | USD | duration | 2007-09-29 – 2026-06-27 | 234 |
| Net income | `NetIncomeLoss` | USD | duration | 2007-09-29 – 2026-06-27 | 338 |
| Basic EPS | `EarningsPerShareBasic` | USD/shares | duration | 2007-09-29 – 2026-06-27 | 338 |
| Diluted EPS | `EarningsPerShareDiluted` | USD/shares | duration | 2007-09-29 – 2026-06-27 | 338 |
| Total assets | `Assets` | USD | **instant** | 2008-09-27 – 2026-06-27 | 146 |
| Total liabilities | `Liabilities` | USD | **instant** | 2008-09-27 – 2026-06-27 | 144 |
| Stockholders' equity | `StockholdersEquity` | USD | **instant** | 2006-09-30 – 2026-06-27 | 264 |
| Cash & equivalents (ending balance) | `CashAndCashEquivalentsAtCarryingValue` | USD | **instant** | 2006-09-30 – 2026-06-27 | 228 |
| Operating cash flow | `NetCashProvidedByUsedInOperatingActivities` | USD | duration | 2007-09-29 – 2026-06-27 | 134 |
| Share buybacks | `PaymentsForRepurchaseOfCommonStock` | USD | duration | 2011-09-24 – 2026-06-27 | 126 |
| Shares outstanding (balance-sheet basis) | `CommonStockSharesOutstanding` | shares | **instant** | 2008-09-27 – 2026-06-27 | 144 |
| Shares outstanding (cover-page basis) | `dei:EntityCommonStockSharesOutstanding` | shares | **instant** | Through the latest 10-Q/10-K cover date | — |
| Public float | `dei:EntityPublicFloat` | USD | **instant** | One point per annual 10-K | — |

> The point-count differences are themselves informative: 338 (reported every
> quarter plus comparative periods) vs 134 (reported only on half-year/annual
> cumulative bases) — **not every metric has an independent data point every
> quarter.**

## 5. Confusion checklist (read before building any SQL/metrics layer)

### 5.1 One metric, several tags (revenue is the worst case)
AAPL's "revenue" has used 3 tags over time (first three rows of Section 4):
`SalesRevenueNet` before FY2018, `RevenueFromContractWithCustomerExcludingAssessedTax`
after ASC 606, plus 11 points on the generic `Revenues` tag in between. IFRS
filers use a different set entirely (`ifrs-full:Revenue`). **The metrics
layer must maintain a metric → tag-priority mapping table**, resolving by
priority and stitching time series across migrations — otherwise a query for
2016 revenue returns nothing or the wrong series. *(Corollary learned in
production: for "latest value" queries, pick the newest period across ALL
mapped tags — priority order alone returned a four-year-old figure when a
company migrated to a lower-priority tag.)*

### 5.2 `fy`/`fp` describe the FILING's period, not the data's
Measured: the point `start=2006-10-01, end=2007-09-29` (FY2007 net income)
carries `fy=2009, fp=FY` — because it appeared as a comparative period inside
the 2009 10-K. **Filter by year/quarter using `start`/`end` only; using
`fy`/`fp` shifts everything by whole periods.**

### 5.3 The same period appears multiple times — with differing values
Of `NetIncomeLoss`'s 338 points, 112 (start, end) periods occur more than
once — the same number is re-reported by the original filing, amendments
(10-K/A), and later years' comparative columns. Measured, FY2007 net income:
the 10-K reports 3,496M; the 10-K/A reports **3,495M**. Recommended
deduplication: group by `(tag, unit, start, end)` and keep the row with the
latest `filed`; or keep only `frame`-marked points (below). *(Production
addendum: deduplicate AFTER filtering to annual-report forms — a DEF 14A
proxy once re-tagged a figure 1000× mis-scaled and, being filed later, outvoted
the 10-K.)*

### 5.4 `frame` marks only the "official representative point"
Only 86 of `NetIncomeLoss`'s 338 points carry `frame`. For each calendar
period the SEC marks exactly one of the duplicate records (these are what the
frames endpoint returns). Good for cross-sectional comparison — but binning
is by **calendar period**, so non-calendar-fiscal-year companies land in the
nearest calendar bin.

### 5.5 instant vs duration: no `start` field means point-in-time
Balance-sheet concepts (`Assets`, `StockholdersEquity`,
`CommonStockSharesOutstanding`, cash **balances**) are instant-type — their
points have no `start`. Income-statement/cash-flow concepts are
duration-type. Two traps: treating `CashAndCashEquivalentsAtCarryingValue`
(an ending balance) as a cash flow; and forgetting the `I` suffix when
requesting frames for instant concepts.

### 5.6 Standalone Q4 barely exists — compute it
Among short-period (≤120-day) points for income-statement metrics, AAPL has
only 11 Q4-shaped periods, all from 2009–2012 (annual-report footnotes/8-Ks
of that era), and none since. Reason: the 10-K reports the full year only,
so Q4 = FY − Q1 − Q2 − Q3 **must be computed in the metrics layer**. Frames'
`CY####Q4` duration data is equally sparse.

### 5.7 Fiscal year ≠ calendar year
AAPL's fiscal year ends in late September (FY2025 = 2024-09-29 –
2025-09-27). Frames bins by calendar period with 365±30 / 91±30-day
windows, so companies inside one frame have different actual start/end
dates; cross-company "same period" comparisons are approximate. *(Corollary:
fiscal-year labels run AHEAD of the calendar — Microsoft's FY2026 ended June
2026 and is filed. A router must never treat a specific fiscal-year label as
"the future".)*

### 5.8 "Shares outstanding" has three distinct bases
`dei:EntityCommonStockSharesOutstanding` (**cover date**, per 10-Q/10-K
submission), `us-gaap:CommonStockSharesOutstanding` and
`CommonStockSharesIssued` (**balance-sheet date**; issued ≥ outstanding), and
`WeightedAverageNumberOfBasic/DilutedSharesOutstanding` (**period-weighted**,
EPS-specific, duration-type). Market cap, EPS math, and disclosure checks
each need their own basis — never mix them.

### 5.9 Historical values are preserved as filed — splits are not restated
The same period's EPS differing by several × across filings is normal.
Measured, AAPL FY2019 diluted EPS: the 2019 10-K reports **11.89**; the
2020/2021 10-Ks' comparative columns report **2.97** (restated after the
August 2020 4-for-1 split). The API performs no retroactive adjustment; old
filings' points remain in the array as filed. **For per-share and share-count
metrics, always take "the latest-filed record for the period" when building
time series, or the series breaks at every split.**

### 5.10 Unit strings are irregular; one tag can span multiple units
Year-duration units: AAPL writes `Year`, SAP writes `Y`; the same tag
(`FiniteLivedIntangibleAssetsUsefulLifeMaximum`) appears under both `pure`
and `Year` unit arrays in AAPL's data. Ratios/tax rates use `pure`.
**Filter by unit before aggregating — never concatenate all of a tag's unit
arrays.**

### 5.11 Currencies beyond USD
Foreign private issuers report in local currency (345 of SAP's tags are in
`EUR`), and exchange-rate units like `USD/EUR` exist. Any future non-US
coverage must carry a currency dimension on monetary aggregation.

### 5.12 Company extension tags are not in the API
Per-product / per-segment revenue is commonly reported under custom extension
tags (`aapl:…`), which no XBRL endpoint returns. Questions like "iPhone
revenue" cannot be answered by the SQL/metrics layer — they must route to the
RAG (document) line or state the data boundary explicitly. This is exactly
the question-routing design FinSignal implements.

## 6. Direct schema recommendations for FinSignal

A single table suffices to start:
`xbrl_facts(cik, taxonomy, tag, unit, start, end, val, accn, fy, fp, form,
filed, frame)` with unique key
`(cik, taxonomy, tag, unit, start, end, accn)`. On top of it, two views —
`facts_dedup` (latest `filed` per period) and `facts_canonical`
(`frame`-marked points only) — plus a `metric_map(metric, taxonomy, tag,
priority)` table for the multi-tag problem in 5.1. Ingest from the nightly
`companyfacts.zip` bundle rather than crawling the API per company.

*(This is the schema FinSignal shipped, with two production hardenings on
top: fact selection filters to annual-report forms before deduplication —
see 5.3 — and "latest" resolution scans across all mapped tags — see 5.1.)*

---

*Source: SEC EDGAR APIs (data.sec.gov), measured 2026-08-12. Official docs:
sec.gov/search-filings/edgar-application-programming-interfaces.*
