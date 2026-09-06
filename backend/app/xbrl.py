"""XBRL numeric data layer: ingest SEC companyfacts into Neon (Phase 2).

Implements the data layer from docs/data-dictionary.md. One official API call
per company fetches EVERY standard-taxonomy financial figure it has ever
reported (~500 tags, ~20 years for a large filer). The pitfalls documented in
the data dictionary are encoded here or in the schema:

- §5.3/§5.9 duplicate reporting & un-restated splits -> facts_dedup view
  (latest ``filed`` wins per period)
- §5.1 one metric, many tags over time -> metric_map priority table (METRICS)
- §5.5 instant concepts have no ``start`` -> nullable start_date
- §5.10 unit strings are messy -> unit kept verbatim; queries filter by unit
- §0    values are RAW units (dollars are dollars, not thousands)

Usage:
    python -m app.xbrl AAPL          # ingest facts for a ticker
    python -m app.xbrl AAPL MSFT
"""

from __future__ import annotations

import json
import sys
import time
from typing import Callable, Iterator

from .config import Settings, get_settings
from .db import connect
from .edgar import _get, resolve_ticker
from .logging_utils import log_event

Progress = Callable[[str], None]


def _noop(_: str) -> None:  # pragma: no cover - trivial
    pass


# Metric registry: canonical metric name -> ordered tag candidates (priority),
# concept kind, and the unit to query. Mirrors data-dictionary §4/§5.1.
METRICS: dict[str, dict] = {
    "revenue": {
        "tags": [("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                 ("us-gaap", "Revenues"),
                 ("us-gaap", "SalesRevenueNet"),
                 ("ifrs-full", "Revenue")],
        "kind": "duration", "unit": "USD",
    },
    "cost_of_revenue": {
        "tags": [("us-gaap", "CostOfGoodsAndServicesSold"),
                 ("us-gaap", "CostOfRevenue"),
                 ("us-gaap", "CostOfSales"),
                 ("us-gaap", "CostOfGoodsSold")],
        "kind": "duration", "unit": "USD",
    },
    "gross_profit": {
        "tags": [("us-gaap", "GrossProfit")],
        "kind": "duration", "unit": "USD",
    },
    "research_and_development": {
        "tags": [("us-gaap", "ResearchAndDevelopmentExpense")],
        "kind": "duration", "unit": "USD",
    },
    "operating_income": {
        "tags": [("us-gaap", "OperatingIncomeLoss")],
        "kind": "duration", "unit": "USD",
    },
    "pretax_income": {
        "tags": [("us-gaap",
                  "IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
                  "ExtraordinaryItemsNoncontrollingInterest"),
                 ("us-gaap",
                  "IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
                  "MinorityInterestAndIncomeLossFromEquityMethodInvestments")],
        "unit": "USD", "kind": "duration",
    },
    "net_income": {
        "tags": [("us-gaap", "NetIncomeLoss")],
        "kind": "duration", "unit": "USD",
    },
    "eps_basic": {
        "tags": [("us-gaap", "EarningsPerShareBasic")],
        "kind": "duration", "unit": "USD/shares",
    },
    "eps_diluted": {
        "tags": [("us-gaap", "EarningsPerShareDiluted")],
        "kind": "duration", "unit": "USD/shares",
    },
    "total_assets": {
        "tags": [("us-gaap", "Assets")],
        "kind": "instant", "unit": "USD",
    },
    "total_liabilities": {
        "tags": [("us-gaap", "Liabilities")],
        "kind": "instant", "unit": "USD",
    },
    "stockholders_equity": {
        "tags": [("us-gaap", "StockholdersEquity")],
        "kind": "instant", "unit": "USD",
    },
    "cash_and_equivalents": {
        "tags": [("us-gaap", "CashAndCashEquivalentsAtCarryingValue")],
        "kind": "instant", "unit": "USD",
    },
    "operating_cash_flow": {
        "tags": [("us-gaap", "NetCashProvidedByUsedInOperatingActivities")],
        "kind": "duration", "unit": "USD",
    },
    "share_buybacks": {
        "tags": [("us-gaap", "PaymentsForRepurchaseOfCommonStock")],
        "kind": "duration", "unit": "USD",
    },
    "shares_outstanding": {
        "tags": [("dei", "EntityCommonStockSharesOutstanding"),
                 ("us-gaap", "CommonStockSharesOutstanding")],
        "kind": "instant", "unit": "shares",
    },
    # --- balance-sheet line items (FinanceBench failure analysis: quick/
    # current ratio, working capital, DPO-style questions need these) ---
    "current_assets": {
        "tags": [("us-gaap", "AssetsCurrent")],
        "kind": "instant", "unit": "USD",
    },
    "current_liabilities": {
        "tags": [("us-gaap", "LiabilitiesCurrent")],
        "kind": "instant", "unit": "USD",
    },
    "inventory": {
        "tags": [("us-gaap", "InventoryNet"),
                 ("us-gaap", "InventoryFinishedGoodsNetOfReserves")],
        "kind": "instant", "unit": "USD",
    },
    "accounts_payable": {
        "tags": [("us-gaap", "AccountsPayableCurrent"),
                 ("us-gaap", "AccountsPayableTradeCurrent")],
        "kind": "instant", "unit": "USD",
    },
    "accounts_receivable": {
        "tags": [("us-gaap", "AccountsReceivableNetCurrent"),
                 ("us-gaap", "ReceivablesNetCurrent")],
        "kind": "instant", "unit": "USD",
    },
    "ppe_net": {
        "tags": [("us-gaap", "PropertyPlantAndEquipmentNet")],
        "kind": "instant", "unit": "USD",
    },
    "long_term_debt": {
        "tags": [("us-gaap", "LongTermDebtNoncurrent"),
                 ("us-gaap", "LongTermDebt")],
        "kind": "instant", "unit": "USD",
    },
    # --- income/cash-flow items ---
    "capex": {
        "tags": [("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
                 ("us-gaap", "PaymentsToAcquireProductiveAssets")],
        "kind": "duration", "unit": "USD",
    },
    "depreciation_amortization": {
        "tags": [("us-gaap", "DepreciationDepletionAndAmortization"),
                 ("us-gaap", "DepreciationAmortizationAndAccretionNet"),
                 ("us-gaap", "Depreciation")],
        "kind": "duration", "unit": "USD",
    },
    "sga_expense": {
        "tags": [("us-gaap", "SellingGeneralAndAdministrativeExpense")],
        "kind": "duration", "unit": "USD",
    },
    "interest_expense": {
        "tags": [("us-gaap", "InterestExpense")],
        "kind": "duration", "unit": "USD",
    },
    "dividends_paid": {
        "tags": [("us-gaap", "PaymentsOfDividends"),
                 ("us-gaap", "PaymentsOfDividendsCommonStock")],
        "kind": "duration", "unit": "USD",
    },
}


def registry_tags() -> set[tuple[str, str]]:
    """All (taxonomy, tag) pairs the metric registry can ever query."""

    return {t for spec in METRICS.values() for t in spec["tags"]}


def parse_companyfacts(data: dict,
                       wanted: set[tuple[str, str]] | None = None,
                       ) -> Iterator[tuple]:
    """Yield fact rows from a companyfacts JSON payload.

    Row: (cik, taxonomy, tag, unit, start, end, val, accn, fy, fp, form,
    filed, frame). Points missing mandatory fields are skipped (defensive —
    the API is well-formed in practice). ``wanted`` restricts output to those
    (taxonomy, tag) pairs — used for benchmark companies, where only the
    metric registry's tags are ever queried and full history would be
    millions of rows across 31 filers.
    """

    cik = int(data["cik"])
    for taxonomy, tags in (data.get("facts") or {}).items():
        for tag, obj in tags.items():
            if wanted is not None and (taxonomy, tag) not in wanted:
                continue
            for unit, points in (obj.get("units") or {}).items():
                for p in points:
                    if p.get("end") is None or p.get("val") is None \
                            or not p.get("accn") or not p.get("filed"):
                        continue
                    yield (
                        cik, taxonomy, tag, unit,
                        p.get("start"), p["end"], p["val"], p["accn"],
                        p.get("fy"), p.get("fp"), p.get("form"),
                        p["filed"], p.get("frame"),
                    )


def dedupe_rows(rows: Iterator[tuple]) -> list[tuple]:
    """Drop exact natural-key duplicates within one payload (keep first)."""

    seen: set[tuple] = set()
    out: list[tuple] = []
    for r in rows:
        key = (r[0], r[1], r[2], r[3], r[4], r[5], r[7])  # ...start,end,accn
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def store_filing_docs(cik: int, settings: Settings, deep: bool = False,
                      progress: Progress = _noop) -> int:
    """Upsert accession -> primary-document mappings from EDGAR submissions.

    Powers iXBRL-viewer provenance links (metrics.filing_url). ``deep``
    pages beyond the ~1000-row "recent" window — needed for the decade-old
    filings the FinanceBench benchmark pins.
    """

    progress("Fetching filing document map…")
    subs = json.loads(_get(
        f"https://data.sec.gov/submissions/CIK{cik:010d}.json"))
    blocks = [subs["filings"]["recent"]]
    if deep:
        for extra in subs["filings"].get("files", []):
            time.sleep(0.15)  # stay well under SEC's 10 req/s
            blocks.append(json.loads(
                _get(f"https://data.sec.gov/submissions/{extra['name']}")))
    rows = [
        (cik, acc, doc)
        for b in blocks
        for acc, doc in zip(b["accessionNumber"], b["primaryDocument"])
        if doc
    ]
    with connect(settings) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO filing_docs (cik, accn, primary_doc)
            VALUES (%s, %s, %s)
            ON CONFLICT (cik, accn)
            DO UPDATE SET primary_doc = EXCLUDED.primary_doc
            """,
            rows,
        )
        conn.commit()
    return len(rows)


def seed_metric_map(settings: Settings) -> None:
    """Upsert the METRICS registry into the metric_map table."""

    rows = [
        (metric, taxonomy, tag, priority)
        for metric, spec in METRICS.items()
        for priority, (taxonomy, tag) in enumerate(spec["tags"], start=1)
    ]
    with connect(settings) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO metric_map (metric, taxonomy, tag, priority)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (metric, taxonomy, tag)
            DO UPDATE SET priority = EXCLUDED.priority
            """,
            rows,
        )
        conn.commit()


def ingest_facts(
    ticker: str,
    settings: Settings | None = None,
    progress: Progress = _noop,
) -> dict:
    """Fetch + store all XBRL facts for a ticker (idempotent per company)."""

    settings = settings or get_settings()
    ticker = ticker.strip().upper()

    progress(f"Resolving {ticker} (SEC registry)…")
    entry = resolve_ticker(ticker)
    cik = int(entry["cik"])

    progress(f"Fetching companyfacts for {entry['title']}…")
    data = json.loads(_get(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
    ))

    # Registry-filtered: the metrics layer can only ever query the tags in
    # METRICS, and a large filer's FULL history is 50k+ rows — most of the
    # onboarding minute was that executemany over the WAN. Same filter the
    # benchmark ingester uses; adding registry tags later just needs a
    # re-ingest (idempotent).
    rows = dedupe_rows(parse_companyfacts(data, wanted=registry_tags()))
    progress(f"Storing {len(rows):,} facts…")
    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM xbrl_facts WHERE cik = %s", (cik,))
        cur.executemany(
            """
            INSERT INTO xbrl_facts
                (cik, taxonomy, tag, unit, start_date, end_date, val,
                 accn, fy, fp, form, filed, frame)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
        conn.commit()
    seed_metric_map(settings)
    store_filing_docs(cik, settings, progress=progress)
    try:  # fresh facts must be visible immediately, not after the TTL
        from .metrics import _FACTS_CACHE
        _FACTS_CACHE.clear()
    except Exception:  # noqa: BLE001
        pass

    result = {"ticker": ticker, "cik": cik, "company": entry["title"],
              "facts": len(rows)}
    log_event("xbrl_ingested", settings.log_path, **result)
    return result


if __name__ == "__main__":  # pragma: no cover - operational entry point
    for t in sys.argv[1:] or ["AAPL"]:
        r = ingest_facts(t, progress=print)
        print(f"[{r['ticker']}] {r['company']}: {r['facts']:,} facts stored")
