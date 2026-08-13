"""Download the latest 10-K for each ticker from SEC EDGAR into data/raw/.

Thin CLI over :mod:`app.edgar` — any US-listed ticker works (resolved via
SEC's official company mapping, no hardcoded CIK list).

Usage:
    python -m scripts.download_filings            # AAPL MSFT TSLA (demo trio)
    python -m scripts.download_filings NVDA JPM   # any tickers
"""

from __future__ import annotations

import sys
import time

from app.edgar import EdgarError, download_10k, latest_10k, resolve_ticker

DEFAULT_TICKERS = ["AAPL", "MSFT", "TSLA"]


def main(tickers: list[str]) -> None:
    for i, ticker in enumerate(tickers):
        if i:
            time.sleep(0.5)  # stay far under SEC's rate limit
        try:
            entry = resolve_ticker(ticker)
            meta = latest_10k(entry["cik"])
            print(f"[{ticker}] {entry['title']} — 10-K filed {meta['filing_date']}")
            out = download_10k(ticker, entry["cik"])
            print(f"[{ticker}]   -> {out}  ({out.stat().st_size:,} bytes)")
        except EdgarError as exc:
            print(f"[{ticker}] SKIPPED: {exc}")


if __name__ == "__main__":
    main([t.upper() for t in sys.argv[1:]] or DEFAULT_TICKERS)
