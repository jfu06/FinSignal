"""Backfill XBRL facts for the FinanceBench benchmark companies.

One companyfacts API call per company returns its ENTIRE reporting history
(2009+), so "historical" facts need no special handling — but full history
for 31 large filers would be millions of rows, and the metrics layer only
ever queries the tags in the metric registry. So benchmark companies are
ingested FILTERED to registry tags (~a few thousand rows each).

Companies whose CIK already has facts in the DB (live-onboarded tickers like
AMD/AMZN/MSFT, ingested unfiltered) are skipped — their history is already
complete. Idempotent and resumable.

Usage:  python -m eval.ingest_benchmark_facts            # missing only
        python -m eval.ingest_benchmark_facts --refresh  # re-pull filtered
        # (--refresh after adding registry tags: filtered companies need a
        #  re-pull to pick the new tags up; full-history live companies are
        #  left alone, recognized by their row count)
"""

from __future__ import annotations

import json
import sys
import time

from app.config import get_settings
from app.db import connect
from app.edgar import _get
from app.logging_utils import log_event
from app.xbrl import (
    dedupe_rows,
    parse_companyfacts,
    registry_tags,
    seed_metric_map,
    store_filing_docs,
)

from .benchmark_data import load_benchmark


FULL_HISTORY_MIN_ROWS = 10_000  # live companies (unfiltered ingest) exceed this


def _fact_counts(settings) -> dict[int, int]:
    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute("SELECT cik, count(*) FROM xbrl_facts GROUP BY cik")
        return dict(cur.fetchall())


def main(refresh: bool = False) -> int:
    settings = get_settings()
    docs, _ = load_benchmark()
    companies = sorted({(d.company, d.cik) for d in docs})
    counts = _fact_counts(settings)
    # Skip CIKs that already have facts; with --refresh, re-pull the FILTERED
    # ones (their row counts are small) but never touch a full-history ingest.
    have = {cik for cik, n in counts.items()
            if not refresh or n >= FULL_HISTORY_MIN_ROWS}
    wanted = registry_tags()
    failures: list[tuple[str, str]] = []

    for i, (company, cik_str) in enumerate(companies, 1):
        cik = int(cik_str)
        if cik in have:
            print(f"[{i}/{len(companies)}] {company}: facts already present, skip")
            continue
        print(f"[{i}/{len(companies)}] {company} (CIK {cik})…")
        try:
            data = json.loads(_get(
                f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"))
            rows = dedupe_rows(parse_companyfacts(data, wanted=wanted))
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
            n_docs = store_filing_docs(cik, settings, deep=True)
            print(f"  stored {len(rows):,} registry-tag facts, "
                  f"{n_docs:,} filing-doc mappings")
            log_event("xbrl_ingested", settings.log_path,
                      ticker=company, cik=cik, company=company,
                      facts=len(rows), filtered="registry")
        except Exception as exc:  # noqa: BLE001 — record, keep going
            failures.append((company, f"{type(exc).__name__}: {exc}"))
            print(f"  FAILED: {exc}")
        time.sleep(0.3)  # polite EDGAR pacing

    seed_metric_map(settings)  # new registry entries -> metric_map
    print(f"\nDone; {len(failures)} failures.")
    for name, err in failures:
        print(f"  {name}: {err}")
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover - operational entry point
    raise SystemExit(main(refresh="--refresh" in sys.argv))
