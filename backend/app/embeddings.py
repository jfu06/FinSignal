"""Local embeddings via sentence-transformers (design-doc §4 local option).

Model: ``intfloat/multilingual-e5-small`` (384-dim, multilingual). The corpus
is English 10-K text but user questions may be Chinese (see the sample
question in requirements.md), so the embedding model must be cross-lingual —
an English-only model silently breaks retrieval for Chinese questions.

Two practical details are handled here so callers can't get them wrong:

1. **Asymmetric prefixes** — e5 models REQUIRE ``"query: "`` on the question
   side and ``"passage: "`` on the corpus side (bge-en models instead want an
   instruction prefix on the query only). Missing prefixes silently cost
   recall, so the split is baked into ``embed_query`` vs ``embed_passages``
   and keyed off the model name.
2. **Normalization** — embeddings are L2-normalized, so pgvector's cosine
   distance behaves as expected.

The model is loaded lazily and cached per process; first use downloads the
weights from the Hugging Face hub (~450 MB for e5-small).

NOTE: if you change the embedding model you MUST re-ingest the corpus
(``python -m app.ingest``) — stored passage vectors and query vectors have to
come from the same model.
"""

from __future__ import annotations

from functools import lru_cache

from .config import Settings, get_settings

# Query-side instruction for BGE v1.5 English retrieval models.
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
# e5 family prefixes (both sides).
E5_QUERY_PREFIX = "query: "
E5_PASSAGE_PREFIX = "passage: "


@lru_cache(maxsize=1)
def _load_model(model_name: str):
    # Imported lazily: torch is heavy and tests that only exercise chunking or
    # config should not pay the import cost.
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(model_name)


def _prefixes(model_name: str) -> tuple[str, str]:
    """Return (query_prefix, passage_prefix) for the given model family."""

    name = model_name.lower()
    if "e5" in name:
        return E5_QUERY_PREFIX, E5_PASSAGE_PREFIX
    if "bge" in name:
        return BGE_QUERY_PREFIX, ""
    return "", ""


def embed_passages(
    texts: list[str], settings: Settings | None = None
) -> list[list[float]]:
    """Embed corpus passages (passage-side prefix applied if the model needs one)."""

    settings = settings or get_settings()
    _, passage_prefix = _prefixes(settings.embedding_model)
    model = _load_model(settings.embedding_model)
    vectors = model.encode(
        [passage_prefix + t for t in texts],
        normalize_embeddings=True,
        batch_size=128,
        show_progress_bar=len(texts) > 200,
    )
    return [v.tolist() for v in vectors]


def embed_query(text: str, settings: Settings | None = None) -> list[float]:
    """Embed a search query (query-side prefix applied if the model needs one)."""

    settings = settings or get_settings()
    query_prefix, _ = _prefixes(settings.embedding_model)
    model = _load_model(settings.embedding_model)
    vector = model.encode([query_prefix + text], normalize_embeddings=True)[0]
    return vector.tolist()
