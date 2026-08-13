"""Neon Postgres access (pgvector).

Thin helpers around psycopg 3: open a connection from ``DATABASE_URL`` with the
pgvector adapter registered, and apply ``schema.sql``. Higher-level read/write
helpers (ingest, retrieval) live in their own modules.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

from .config import Settings, get_settings

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect(settings: Settings | None = None) -> psycopg.Connection:
    """Open a psycopg connection to Neon with pgvector registered.

    The caller owns the connection (use it as a context manager). pgvector must
    be registered per-connection so ``vector`` columns round-trip as Python
    lists / numpy arrays.
    """

    settings = settings or get_settings()
    conn = psycopg.connect(settings.database_url)
    register_vector(conn)
    return conn


def init_schema(settings: Settings | None = None) -> None:
    """Create the extension and the three tables (idempotent).

    Uses a plain connection (no pgvector adapter): ``register_vector`` needs
    the ``vector`` type to already exist, but it is this DDL that creates it.
    """

    settings = settings or get_settings()
    ddl = _SCHEMA_PATH.read_text(encoding="utf-8")
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()


if __name__ == "__main__":  # pragma: no cover - operational entry point
    init_schema()
    print("Schema applied to Neon (chunks / claims / test_cases).")
