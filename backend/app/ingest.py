"""Ingestion: chunk local 10-K text files, embed once, store in Neon.

Design-doc §3 steps 1-3. Reads ``data/raw/{TICKER}_10K.txt`` (produced by
``scripts/download_filings.py``), chunks each filing (``app.chunking``),
embeds all chunks locally (``app.embeddings``), and writes rows to the
``chunks`` table. Re-ingesting a document replaces its rows (idempotent).

Usage:
    python -m app.ingest              # ingest every *_10K.txt in data/raw
    python -m app.ingest AAPL MSFT    # only these tickers
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pgvector.psycopg import Vector

from .chunking import chunk_text
from .config import Settings, get_settings
from .db import connect
from .embeddings import embed_passages
from .logging_utils import log_event

DATA_RAW = Path(__file__).resolve().parent.parent / "data" / "raw"


def _doc_id(ticker: str, meta_path: Path) -> str:
    """Stable doc id, e.g. AAPL_10K_2024 (filing year from the meta file)."""

    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        year = (meta.get("filing_date") or "")[:4]
        if year:
            return f"{ticker}_10K_{year}"
    return f"{ticker}_10K"


def ingest_file(path: Path, settings: Settings) -> int:
    """Chunk + embed + store one filing. Returns the number of chunks stored."""

    ticker = path.stem.split("_")[0].upper()
    doc_id = _doc_id(ticker, path.with_name(path.stem + ".meta.json"))
    text = path.read_text(encoding="utf-8")

    raw_chunks = chunk_text(text)
    if not raw_chunks:
        raise ValueError(f"{path.name}: no chunks produced — empty/corrupt file?")

    print(f"[{ticker}] {len(raw_chunks)} chunks from {path.name}; embedding…")
    vectors = embed_passages([c.text for c in raw_chunks], settings)

    rows = [
        (
            f"{doc_id}_{i:04d}",          # chunk_id
            doc_id,
            ticker,
            c.section,
            c.text,
            Vector(v),
        )
        for i, (c, v) in enumerate(zip(raw_chunks, vectors))
    ]

    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM chunks WHERE doc_id = %s", (doc_id,))
        cur.executemany(
            """
            INSERT INTO chunks (chunk_id, doc_id, ticker, section, text, embedding)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
        conn.commit()

    log_event(
        "ingest",
        settings.log_path,
        doc_id=doc_id,
        ticker=ticker,
        source_file=path.name,
        num_chunks=len(rows),
    )
    print(f"[{ticker}] stored {len(rows)} chunks as doc_id={doc_id}")
    return len(rows)


def run_ingest(tickers: list[str] | None = None) -> int:
    """Ingest all (or selected) downloaded filings. Returns total chunk count."""

    settings = get_settings()
    files = sorted(DATA_RAW.glob("*_10K.txt"))
    if tickers:
        wanted = {t.upper() for t in tickers}
        files = [f for f in files if f.stem.split("_")[0].upper() in wanted]
    if not files:
        raise SystemExit(
            "No corpus files found in data/raw/. "
            "Run `python -m scripts.download_filings` first."
        )
    return sum(ingest_file(f, settings) for f in files)


if __name__ == "__main__":  # pragma: no cover - operational entry point
    total = run_ingest(sys.argv[1:] or None)
    print(f"Done. {total} chunks in Neon.")
