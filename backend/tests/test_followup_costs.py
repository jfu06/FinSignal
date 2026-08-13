"""Unit tests for follow-up condensation and cost accounting."""

from __future__ import annotations

import json

from app.costs import aggregate, price_for
from app.followup import condense_followup
from tests.llm_fakes import FakeAnthropic, make_settings, tool_response

HISTORY = [{"question": "What drove Apple's revenue growth?",
            "summary": "Services led fiscal 2025 growth."}]


class TestCondenseFollowup:
    def test_empty_history_no_llm_call(self, tmp_path):
        out = condense_followup("What about margins?", [],
                                settings=make_settings(tmp_path))
        assert out == "What about margins?"
        assert FakeAnthropic.calls == []

    def test_followup_rewritten_with_history(self, tmp_path):
        FakeAnthropic.queue = [tool_response(
            {"standalone_question": "What were Apple's margins in fiscal 2025?"})]
        out = condense_followup("What about margins?", HISTORY,
                                settings=make_settings(tmp_path))
        assert out == "What were Apple's margins in fiscal 2025?"
        assert len(FakeAnthropic.calls) == 1
        # history is in the prompt
        assert "Services led fiscal 2025 growth." in \
            FakeAnthropic.calls[0]["messages"][0]["content"]

    def test_llm_failure_falls_back_to_raw_message(self, tmp_path):
        FakeAnthropic.queue = []  # fake client will blow up on pop
        out = condense_followup("和去年比呢？", HISTORY,
                                settings=make_settings(tmp_path))
        assert out == "和去年比呢？"

    def test_empty_rewrite_falls_back(self, tmp_path):
        FakeAnthropic.queue = [tool_response({"standalone_question": "  "})]
        out = condense_followup("follow up", HISTORY,
                                settings=make_settings(tmp_path))
        assert out == "follow up"


class TestCosts:
    def _event(self, day, stage, model, tin, tout):
        return json.dumps({
            "ts": f"{day}T10:00:00+00:00", "event": "llm_call",
            "stage": stage, "model": model,
            "input_tokens": tin, "output_tokens": tout,
        }) + "\n"

    def test_aggregate_by_day_stage_model(self, tmp_path):
        log = tmp_path / "events.jsonl"
        log.write_text(
            self._event("2026-08-13", "generation", "claude-sonnet-5", 10_000, 1_000)
            + self._event("2026-08-13", "generation", "claude-sonnet-5", 10_000, 1_000)
            + self._event("2026-08-13", "assess", "claude-haiku-4-5", 2_000, 100)
            + self._event("2026-08-12", "judge", "claude-sonnet-5", 5_000, 500)
        )
        rows = aggregate(log, day="2026-08-13")
        assert len(rows) == 2
        gen = next(r for r in rows if r["stage"] == "generation")
        assert gen["calls"] == 2
        assert gen["input_tokens"] == 20_000
        # 20k*3/1e6 + 2k*15/1e6 = 0.06 + 0.03
        assert gen["est_cost_usd"] == 0.09

    def test_events_without_usage_counted_as_zero_tokens(self, tmp_path):
        log = tmp_path / "events.jsonl"
        log.write_text(json.dumps({
            "ts": "2026-08-13T10:00:00+00:00", "event": "llm_call",
            "stage": "judge", "model": "claude-sonnet-5",
        }) + "\n")
        rows = aggregate(log)
        assert rows[0]["calls"] == 1 and rows[0]["est_cost_usd"] == 0.0

    def test_price_prefix_matching(self):
        assert price_for("claude-haiku-4-5-20251001") == (1.0, 5.0)
        assert price_for("claude-sonnet-5") == (3.0, 15.0)
        assert price_for("unknown-model") == (3.0, 15.0)
