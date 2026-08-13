"""On-demand ticker onboarding: user asks about a company we don't have yet →
fetch its latest 10-K from SEC EDGAR, chunk + embed + store, ready to query.

Flow (each step reports progress via the ``progress`` callback):

    ensure_ticker("NVDA")
      1. already in the chunks table?  -> {"status": "exists"}
      2. resolve ticker -> CIK via SEC's official company mapping
      3. download the latest 10-K (plain text into data/raw/)
      4. ingest (chunk -> embed locally -> Neon)
      -> {"status": "added", "company": ..., "filing_date": ..., "chunks": n}

Honest quality boundary: newly onboarded companies are NOT covered by the
offline golden set — the release gate certifies the pipeline on its eval
corpus, not each new document. The UI surfaces this caveat.
"""

from __future__ import annotations

from typing import Callable

from .config import Settings, get_settings
from .db import connect
from .edgar import EdgarError, download_10k, latest_10k, resolve_ticker
from .ingest import ingest_file
from .logging_utils import log_event

Progress = Callable[[str], None]


class OnboardingError(RuntimeError):
    """User-displayable onboarding refusal (e.g. corpus cap reached)."""


def _noop(_: str) -> None:  # pragma: no cover - trivial
    pass


def known_tickers(settings: Settings | None = None) -> set[str]:
    settings = settings or get_settings()
    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT ticker FROM chunks")
        return {r[0] for r in cur.fetchall()}


def ensure_ticker(
    ticker: str,
    settings: Settings | None = None,
    progress: Progress = _noop,
) -> dict:
    """Make ``ticker`` queryable, downloading + ingesting its 10-K if needed.

    Raises :class:`app.edgar.EdgarError` with a user-displayable message when
    the ticker can't be onboarded (unknown symbol, no 10-K, bad download).
    """

    settings = settings or get_settings()
    ticker = ticker.strip().upper()

    existing = known_tickers(settings)
    if ticker in existing:
        return {"status": "exists", "ticker": ticker}
    # Server-side corpus cap (public-deployment guardrail): embedding compute
    # and DB storage must not grow unboundedly from anonymous onboarding.
    # max_tickers == 0 means unlimited (the default).
    if settings.max_tickers and len(existing) >= settings.max_tickers:
        raise OnboardingError(
            f"Corpus limit reached ({settings.max_tickers} companies). "
            f"Currently available: {', '.join(sorted(existing))}."
        )

    progress("Resolving ticker (SEC registry)…")
    entry = resolve_ticker(ticker)  # raises EdgarError if unknown

    progress(f"Locating the latest 10-K for {entry['title']}…")
    meta = latest_10k(entry["cik"])  # raises EdgarError if no 10-K

    progress(f"Downloading 10-K (filed {meta['filing_date']})…")
    path = download_10k(ticker, entry["cik"])

    progress("Chunking + embedding locally + writing to Neon…")
    num_chunks = ingest_file(path, settings)

    log_event(
        "ticker_onboarded", settings.log_path,
        ticker=ticker, company=entry["title"],
        filing_date=meta["filing_date"], num_chunks=num_chunks,
    )
    return {
        "status": "added",
        "ticker": ticker,
        "company": entry["title"],
        "filing_date": meta["filing_date"],
        "chunks": num_chunks,
    }
