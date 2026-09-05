"""Shared test fakes: a stand-in Anthropic client and small factories.

Used by the mock-based unit tests (no network, no DB, no real LLM).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.models import Chunk


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql://u:p@h/db",
        anthropic_api_key="sk-ant-test",
        llm_model="claude-test",
        assess_model="claude-test-fast",
        judge_provider="anthropic",
        judge_model="",
        openai_api_key="",
        embedding_model="intfloat/multilingual-e5-small",
        embedding_dim=384,
        top_k=6,
        unsupported_rate_threshold=0.04,
        log_path=tmp_path / "events.jsonl",
        access_code="",
        session_query_limit=10,
        daily_query_budget=50,
        max_tickers=10,
    )


def make_chunk(cid: str = "c1") -> Chunk:
    return Chunk(chunk_id=cid, doc_id="AAPL_10K_2025", ticker="AAPL",
                 text="Revenue grew due to Services.", section="Item 7")


class FakeAnthropic:
    """Stands in for anthropic.Anthropic; returns queued responses."""

    queue: list[object] = []
    calls: list[dict] = []

    def __init__(self, **kwargs):  # noqa: ANN003
        pass

    @property
    def messages(self):
        class _M:
            def create(self, **kwargs):  # noqa: ANN003
                FakeAnthropic.calls.append(kwargs)
                return FakeAnthropic.queue.pop(0)

        return _M()


def tool_response(input_data: dict, stop_reason: str = "tool_use"):
    block = SimpleNamespace(type="tool_use", input=input_data)
    return SimpleNamespace(
        content=[block],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )
