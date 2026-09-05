"""Unit tests for the cross-vendor judge routing (no network)."""

from __future__ import annotations

import dataclasses

import pytest

import app.verification as verification
from app.config import ConfigError
from app.models import Claim, Verdict
from app.verification import _judge_model, verify_claims_batch
from tests.llm_fakes import make_chunk, make_settings


def _claims() -> list[Claim]:
    return [Claim(claim_id="q1_claim1", query_id="q1", text="t",
                  cited_chunk_ids=["c1"])]


class TestJudgeModelResolution:
    def test_anthropic_defaults_to_llm_model(self, tmp_path):
        s = make_settings(tmp_path)
        assert _judge_model(s) == "claude-test"

    def test_openai_defaults_to_gpt5_mini(self, tmp_path):
        s = dataclasses.replace(make_settings(tmp_path), judge_provider="openai")
        assert _judge_model(s) == "gpt-5-mini"

    def test_explicit_judge_model_wins(self, tmp_path):
        s = dataclasses.replace(make_settings(tmp_path), judge_model="gpt-4o")
        assert _judge_model(s) == "gpt-4o"


class TestProviderRouting:
    def test_openai_provider_routes_to_openai_judge(self, monkeypatch, tmp_path):
        s = dataclasses.replace(make_settings(tmp_path),
                                judge_provider="openai",
                                openai_api_key="sk-oai-test")
        called = {}

        def fake_openai(prompt, settings):
            called["prompt"] = prompt
            return ({"verdicts": [{"claim_id": "q1_claim1",
                                   "verdict": "SUPPORTED", "reason": "ok"}]},
                    "tool_calls", 100, 20)

        monkeypatch.setattr(verification, "_judge_openai", fake_openai)
        claims = verify_claims_batch(_claims(), {"c1": make_chunk()}, s)
        assert claims[0].verdict is Verdict.SUPPORTED
        assert "q1_claim1" in called["prompt"]

    def test_anthropic_provider_never_touches_openai(self, monkeypatch, tmp_path):
        from tests.llm_fakes import FakeAnthropic, tool_response

        def boom(*a, **k):  # noqa: ANN002, ANN003
            raise AssertionError("openai judge must not be called")

        monkeypatch.setattr(verification, "_judge_openai", boom)
        FakeAnthropic.queue = [tool_response({"verdicts": [
            {"claim_id": "q1_claim1", "verdict": "SUPPORTED", "reason": "ok"}]})]
        claims = verify_claims_batch(_claims(), {"c1": make_chunk()},
                                     make_settings(tmp_path))
        assert claims[0].verdict is Verdict.SUPPORTED

    def test_openai_without_key_raises_config_error(self, tmp_path):
        s = dataclasses.replace(make_settings(tmp_path), judge_provider="openai")
        with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
            verify_claims_batch(_claims(), {"c1": make_chunk()}, s)
