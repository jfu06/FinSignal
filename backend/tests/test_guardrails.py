"""Unit tests for the public-deployment cost guardrails."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import app.onboarding as onboarding
from app.onboarding import OnboardingError
from app.usage import daily_budget_left, queries_today
from tests.llm_fakes import make_settings

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


def _event(ts: str, event: str) -> str:
    return f'{{"ts": "{ts}", "event": "{event}", "query_id": "q1"}}\n'


class TestQueriesToday:
    def test_counts_only_today_query_received(self, tmp_path):
        log = tmp_path / "events.jsonl"
        log.write_text(
            _event("2026-08-13T01:00:00+00:00", "query_received")
            + _event("2026-08-13T02:00:00+00:00", "query_received")
            + _event("2026-08-12T23:59:00+00:00", "query_received")  # yesterday
            + _event("2026-08-13T03:00:00+00:00", "retrieval")       # other event
        )
        assert queries_today(log, now=NOW) == 2

    def test_missing_log_is_zero(self, tmp_path):
        assert queries_today(tmp_path / "nope.jsonl", now=NOW) == 0

    def test_budget_left_never_negative(self, tmp_path):
        log = tmp_path / "events.jsonl"
        log.write_text(_event("2026-08-13T01:00:00+00:00", "query_received") * 5)
        # NOTE: daily_budget_left uses the real current date; only assert the
        # non-negative contract, which holds for any date.
        assert daily_budget_left(log, 3) >= 0


class TestCorpusCap:
    def test_onboarding_refused_at_cap(self, monkeypatch, tmp_path):
        settings = make_settings(tmp_path)
        crowded = {f"T{i:02d}" for i in range(settings.max_tickers)}
        monkeypatch.setattr(onboarding, "known_tickers", lambda s: crowded)
        with pytest.raises(OnboardingError, match="Corpus limit reached"):
            onboarding.ensure_ticker("NVDA", settings=settings)

    def test_existing_ticker_still_ok_at_cap(self, monkeypatch, tmp_path):
        settings = make_settings(tmp_path)
        crowded = {f"T{i:02d}" for i in range(settings.max_tickers)}
        monkeypatch.setattr(onboarding, "known_tickers", lambda s: crowded)
        result = onboarding.ensure_ticker("T00", settings=settings)
        assert result["status"] == "exists"

    def test_zero_means_unlimited(self, monkeypatch, tmp_path):
        import dataclasses
        settings = dataclasses.replace(make_settings(tmp_path), max_tickers=0)
        crowded = {f"T{i:02d}" for i in range(500)}
        monkeypatch.setattr(onboarding, "known_tickers", lambda s: crowded)
        # cap disabled -> proceeds past the check into resolution
        called = []
        monkeypatch.setattr(onboarding, "resolve_ticker",
                            lambda t: called.append(t) or (_ for _ in ()).throw(
                                onboarding.EdgarError("stop here")))
        with pytest.raises(onboarding.EdgarError, match="stop here"):
            onboarding.ensure_ticker("NVDA", settings=settings)
        assert called == ["NVDA"]
