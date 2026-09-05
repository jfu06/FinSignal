"""Retrieval: single pgvector top-k query (design-doc §3 step 4, §4).

Embeds the question locally (BGE query prefix handled by ``app.embeddings``)
and runs ONE cosine-similarity SQL query against the Neon ``chunks`` table:
``ORDER BY embedding <=> :question_embedding LIMIT k``, filtered by ticker.
"""

from __future__ import annotations

import numpy as np
from pgvector.psycopg import Vector

from .config import Settings, get_settings
from .db import connect
from .embeddings import embed_query
from .logging_utils import log_event
from .models import Chunk


def _mmr(rows: list[tuple], k: int, lambda_: float = 0.7) -> list[tuple]:
    """Maximal Marginal Relevance over (…, embedding, distance) rows.

    Greedy: pick the row maximizing λ·relevance − (1−λ)·max-similarity to
    already-picked rows. Coverage questions ("biggest risk factors") need
    breadth across topics — pure top-k returns six paraphrases of the
    strongest match and misses whole risk categories.
    """

    embs = [np.asarray(r[5], dtype=np.float32) for r in rows]
    embs = [e / (np.linalg.norm(e) or 1.0) for e in embs]
    rel = [1.0 - float(r[6]) for r in rows]  # cosine distance -> similarity
    picked: list[int] = []
    while len(picked) < min(k, len(rows)):
        best_i, best_score = -1, -1e9
        for i in range(len(rows)):
            if i in picked:
                continue
            redundancy = max(
                (float(embs[i] @ embs[j]) for j in picked), default=0.0)
            score = lambda_ * rel[i] - (1 - lambda_) * redundancy
            if score > best_score:
                best_i, best_score = i, score
        picked.append(best_i)
    return [rows[i] for i in picked]


def retrieve(
    question: str,
    ticker: str,
    k: int | None = None,
    settings: Settings | None = None,
    query_id: str | None = None,
    doc_id: str | None = None,
    diversify: bool = False,
) -> list[Chunk]:
    """Top-k chunks for ``question`` — ticker-scoped over the live corpus, or
    pinned to one document (benchmark/oracle-document mode) via ``doc_id``.

    ``diversify`` fetches a 3× candidate pool and applies MMR — used for
    coverage/enumeration questions where breadth beats redundant depth.
    """

    settings = settings or get_settings()
    k = k or settings.top_k
    pool = 3 * k if diversify else k

    qvec = Vector(embed_query(question, settings))
    with connect(settings) as conn, conn.cursor() as cur:
        if doc_id:
            cur.execute(
                """
                SELECT chunk_id, doc_id, ticker, section, text,
                       embedding, embedding <=> %s AS dist
                FROM chunks WHERE doc_id = %s
                ORDER BY embedding <=> %s LIMIT %s
                """,
                (qvec, doc_id, qvec, pool),
            )
        else:
            cur.execute(
                """
                SELECT chunk_id, doc_id, ticker, section, text,
                       embedding, embedding <=> %s AS dist
                FROM chunks WHERE ticker = %s AND corpus = 'live'
                ORDER BY embedding <=> %s LIMIT %s
                """,
                (qvec, ticker, qvec, pool),
            )
        rows = cur.fetchall()

    if diversify and len(rows) > k:
        rows = _mmr(rows, k)

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
