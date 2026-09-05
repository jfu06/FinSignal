"""Unit tests for the Anthropic client factory (LangSmith wrapping opt-in)."""

from __future__ import annotations

import anthropic

from app.llm import anthropic_client
from tests.llm_fakes import make_settings


class TestAnthropicClient:
    def test_tracing_off_returns_plain_client(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
        client = anthropic_client(make_settings(tmp_path))
        assert isinstance(client, anthropic.Anthropic)

    def test_tracing_false_string_returns_plain_client(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LANGSMITH_TRACING", "false")
        client = anthropic_client(make_settings(tmp_path))
        assert isinstance(client, anthropic.Anthropic)

    def test_wrap_failure_fails_open(self, monkeypatch, tmp_path):
        # tracing on but the wrapper explodes -> plain client, never an error
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        import langsmith.wrappers as w

        def boom(_client):
            raise RuntimeError("bad key")

        monkeypatch.setattr(w, "wrap_anthropic", boom)
        client = anthropic_client(make_settings(tmp_path))
        assert isinstance(client, anthropic.Anthropic)

    def test_tracing_on_wraps_client(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        import langsmith.wrappers as w

        sentinel = object()
        monkeypatch.setattr(w, "wrap_anthropic", lambda c: sentinel)
        assert anthropic_client(make_settings(tmp_path)) is sentinel
