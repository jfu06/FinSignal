"""Step-1 smoke tests: config, logging, schema, and the verdict mapping.

These run without a database or any API key — they only check that the
skeleton is wired correctly and that the credibility mapping matches the spec.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import ConfigError, get_settings
from app.logging_utils import log_event
from app.models import ClaimStatus, VERDICT_TO_STATUS, Verdict

SCHEMA_SQL = (Path(__file__).resolve().parent.parent / "app" / "schema.sql").read_text()


def test_get_settings_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with pytest.raises(ConfigError):
        get_settings()


def test_get_settings_defaults(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db?sslmode=require")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.delenv("TOP_K", raising=False)
    monkeypatch.delenv("UNSUPPORTED_RATE_THRESHOLD", raising=False)
    monkeypatch.delenv("EMBEDDING_DIM", raising=False)
    s = get_settings()
    assert s.top_k == 6
    assert s.unsupported_rate_threshold == 0.04  # 4% gate from project-requirements
    assert s.embedding_dim == 384  # all-MiniLM-L6-v2 (local sentence-transformers)


def test_log_event_appends_jsonl(tmp_path):
    log_file = tmp_path / "events.jsonl"
    log_event("report_summary", log_file, query_id="q1", unsupported_rate=0.1)
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event"] == "report_summary"
    assert rec["query_id"] == "q1"
    assert "ts" in rec


def test_verdict_status_mapping():
    # Credibility rules: NOT_ENOUGH_INFO -> WARNING, CONTRADICTED -> ERROR.
    assert VERDICT_TO_STATUS[Verdict.SUPPORTED] == ClaimStatus.OK
    assert VERDICT_TO_STATUS[Verdict.NOT_ENOUGH_INFO] == ClaimStatus.WARNING
    assert VERDICT_TO_STATUS[Verdict.CONTRADICTED] == ClaimStatus.ERROR


def test_schema_has_three_tables_and_vector_column():
    assert "CREATE EXTENSION IF NOT EXISTS vector" in SCHEMA_SQL
    assert "vector(384)" in SCHEMA_SQL
    for table in ("chunks", "claims", "test_cases"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in SCHEMA_SQL
