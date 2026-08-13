"""Retrieval: single pgvector top-k query (design-doc §3 step 4, §4).

Embeds the question locally (BGE query prefix handled by ``app.embeddings``)
and runs ONE cosine-similarity SQL query against the Neon ``chunks`` table:
``ORDER BY embedding <=> :question_embedding LIMIT k``, filtered by ticker.
"""

from __future__ import annotations

from pgvector.psycopg import Vector

from .config import Settings, get_settings
from .db import connect
from .embeddings import embed_query
from .logging_utils import log_event
from .models import Chunk


def retrieve(
    question: str,
    ticker: str,
    k: int | None = None,
    settings: Settings | None = None,
    query_id: str | None = None,
) -> list[Chunk]:
    """Return the top-k most similar chunks for ``question`` within ``ticker``."""

    settings = settings or get_settings()
    k = k or settings.top_k

    qvec = Vector(embed_query(question, settings))
    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT chunk_id, doc_id, ticker, section, text
            FROM chunks
            WHERE ticker = %s
            ORDER BY embedding <=> %s
            LIMIT %s
            """,
            (ticker, qvec, k),
        )
        rows = cur.fetchall()

    chunks = [
        Chunk(chunk_id=r[0], doc_id=r[1], ticker=r[2], section=r[3], text=r[4])
        for r in rows
    ]

    if query_id is not None:
        log_event(
            "retrieval",
            settings.log_path,
            query_id=query_id,
            ticker=ticker,
            k=k,
            retrieved_chunk_ids=[c.chunk_id for c in chunks],
        )
    return chunks
