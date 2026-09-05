"""Ingest the FinanceBench 10-K corpus (benchmark-isolated, resumable).

For every 10-K the open-source questions reference (64 docs / 31 companies):
locate the historical filing on EDGAR by (CIK, fiscal year), download the
primary document, extract text, and ingest it into the ``chunks`` table with
``corpus='benchmark'`` and ``doc_id`` = the FinanceBench doc name — so the
scorer can pin retrieval to the exact document each question was written
against (oracle-document mode) and the live product never sees these rows.

Resumable: docs whose chunks are already in the DB are skipped, and raw text
is cached under data/benchmark/raw/. Safe to re-run after a partial failure.

Usage:
    python -m eval.ingest_benchmark            # everything missing
    python -m eval.ingest_benchmark 3M_2018_10K AMD_2015_10K
"""

from __future__ import annotations

import json
import sys
import time

from app.config import get_settings
from app.db import connect
from app.edgar import download_filing_text, filing_for_fy
from app.ingest import ingest_file

from .benchmark_data import EXTRA_DOC_FILES, RAW_DIR, BenchDoc, load_benchmark


def _ingested_doc_ids(settings) -> set[str]:
    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT doc_id FROM chunks WHERE corpus = 'benchmark'")
        return {r[0] for r in cur.fetchall()}


def fetch_doc(doc: BenchDoc) -> None:
    """Download + cache one benchmark filing as text (skip if cached)."""

    out = RAW_DIR / f"{doc.doc_name}.txt"
    if out.exists():
        return
    meta = filing_for_fy(doc.cik, doc.fy, form="10-K")
    text = download_filing_text(doc.cik, meta["accession"], meta["primary_doc"])
    for extra in EXTRA_DOC_FILES.get(doc.doc_name, ()):
        time.sleep(0.2)
        text += "\n\n" + download_filing_text(doc.cik, meta["accession"], extra)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    (RAW_DIR / f"{doc.doc_name}.meta.json").write_text(
        json.dumps({"company": doc.company, "cik": doc.cik, "fy": doc.fy,
                    **meta}, indent=2),
        encoding="utf-8",
    )
    print(f"  fetched {doc.doc_name}: {meta['form']} {meta['accession']} "
          f"report_date={meta['report_date']} ({len(text):,} chars)")


def main(only: list[str] | None = None) -> int:
    settings = get_settings()
    docs, _ = load_benchmark()
    if only:
        wanted = set(only)
        docs = [d for d in docs if d.doc_name in wanted]
        if missing := wanted - {d.doc_name for d in docs}:
            raise SystemExit(f"Unknown doc names: {sorted(missing)}")

    done = _ingested_doc_ids(settings)
    failures: list[tuple[str, str]] = []
    total_chunks = 0
    for i, doc in enumerate(docs, 1):
        if doc.doc_name in done:
            print(f"[{i}/{len(docs)}] {doc.doc_name}: already ingested, skip")
            continue
        print(f"[{i}/{len(docs)}] {doc.doc_name} (CIK {doc.cik}, FY{doc.fy})")
        for attempt in (1, 2):  # Neon serverless drops connections now and then
            try:
                fetch_doc(doc)
                total_chunks += ingest_file(
                    RAW_DIR / f"{doc.doc_name}.txt", settings,
                    corpus="benchmark", doc_id=doc.doc_name,
                )
                break
            except Exception as exc:  # noqa: BLE001 — record, keep going
                if attempt == 1:
                    print(f"  retrying after: {exc}")
                    time.sleep(5)  # ingest_file reconnects on the next call
                else:
                    failures.append((doc.doc_name, f"{type(exc).__name__}: {exc}"))
                    print(f"  FAILED: {exc}")
        time.sleep(0.3)  # polite EDGAR pacing across documents

    print(f"\nDone: {total_chunks} new chunks; {len(failures)} failures.")
    for name, err in failures:
        print(f"  {name}: {err}")
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover - operational entry point
    raise SystemExit(main(sys.argv[1:] or None))
