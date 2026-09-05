"""Unit tests for routing + numeric execution (metrics + LLM mocked)."""

from __future__ import annotations

from datetime import date

import pytest

import app.router as router
from app.metrics import MetricPoint
from app.router import execute_numeric, route_question
from tests.llm_fakes import FakeAnthropic, make_settings, tool_response


def point(metric="revenue", value=416.2e9, fy=2025, unit="USD",
          ticker="AAPL") -> MetricPoint:
    return MetricPoint(
        ticker=ticker, metric=metric, fy=fy, value=value, unit=unit,
        period_start=date(2024, 9, 29), period_end=date(2025, 9, 27),
        accn="0000320193-25-000079", form="10-K",
        source_url="https://sec.gov/x",
    )


@pytest.fixture
def fake_metrics(monkeypatch):
    prev = point(value=391.0e9, fy=2024)
    monkeypatch.setattr(router, "get_metric",
                        lambda t, m, fy=None, settings=None: point(metric=m))
    monkeypatch.setattr(router, "yoy_growth",
                        lambda t, m, fy=None, settings=None:
                        (0.064, point(metric=m), prev))
    monkeypatch.setattr(router, "cagr",
                        lambda t, m, years, settings=None:
                        (0.0181, point(metric=m), prev))
    monkeypatch.setattr(router, "ratio",
                        lambda t, name, fy=None, settings=None:
                        (0.469, point("gross_profit", 195.2e9), point()))
    monkeypatch.setattr(router, "q4_single_quarter",
                        lambda t, m, fy=None, settings=None: point(
                            metric=f"{m}_q4", value=102.5e9))
    monkeypatch.setattr(router, "get_series",
                        lambda t, m, years=5, settings=None:
                        [point(), prev])
    monkeypatch.setattr(router, "compare",
                        lambda ts, m, fy=None, settings=None:
                        [point(ticker="MSFT", value=35.6e9, metric=m),
                         point(ticker="AAPL", value=34.5e9, metric=m)])


class TestExecuteNumeric:
    def test_value_op_renders_with_source(self, fake_metrics, tmp_path):
        out = execute_numeric([{"op": "value", "metric": "revenue"}],
                              "AAPL", make_settings(tmp_path))
        assert out[0]["text"] == "AAPL revenue FY2025: $416.2B"
        assert out[0]["sources"][0]["accn"] == "0000320193-25-000079"

    def test_yoy_op(self, fake_metrics, tmp_path):
        out = execute_numeric([{"op": "yoy", "metric": "revenue"}],
                              "AAPL", make_settings(tmp_path))
        assert "+6.4% YoY" in out[0]["text"]
        assert len(out[0]["sources"]) == 2

    def test_cagr_op(self, fake_metrics, tmp_path):
        out = execute_numeric([{"op": "cagr", "metric": "revenue", "years": 3}],
                              "AAPL", make_settings(tmp_path))
        assert "3-year CAGR: +1.81%" in out[0]["text"]

    def test_ratio_op(self, fake_metrics, tmp_path):
        out = execute_numeric([{"op": "ratio", "metric": "gross_margin"}],
                              "AAPL", make_settings(tmp_path))
        assert "46.9%" in out[0]["text"]

    def test_q4_op_marks_derivation(self, fake_metrics, tmp_path):
        out = execute_numeric([{"op": "q4", "metric": "revenue"}],
                              "AAPL", make_settings(tmp_path))
        assert "$102.5B" in out[0]["text"]

    def test_series_op(self, fake_metrics, tmp_path):
        out = execute_numeric([{"op": "series", "metric": "revenue"}],
                              "AAPL", make_settings(tmp_path))
        assert "FY2024 $391.0B; FY2025 $416.2B" in out[0]["text"]

    def test_average_op(self, fake_metrics, tmp_path):
        out = execute_numeric([{"op": "average", "metric": "revenue"}],
                              "AAPL", make_settings(tmp_path))
        assert "average" in out[0]["text"]
        assert "$403.6B" in out[0]["text"]  # (416.2+391.0)/2

    def test_compare_op_ranks(self, fake_metrics, tmp_path):
        out = execute_numeric(
            [{"op": "compare", "metric": "research_and_development",
              "tickers": ["AAPL", "MSFT"]}], "AAPL", make_settings(tmp_path))
        assert "MSFT FY2025 $35.6B; AAPL FY2025 $34.5B" in out[0]["text"]

    def test_failed_query_dropped_not_crashing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(router, "get_metric",
                            lambda *a, **k: None)
        out = execute_numeric([{"op": "value", "metric": "revenue"}],
                              "AAPL", make_settings(tmp_path))
        assert out == []

    def test_exception_in_one_query_does_not_kill_the_rest(
            self, fake_metrics, monkeypatch, tmp_path):
        def boom(*a, **k):  # noqa: ANN002, ANN003
            raise RuntimeError("db down")
        monkeypatch.setattr(router, "yoy_growth", boom)
        out = execute_numeric(
            [{"op": "yoy", "metric": "revenue"},
             {"op": "value", "metric": "revenue"}],
            "AAPL", make_settings(tmp_path))
        assert len(out) == 1 and "FY2025" in out[0]["text"]

    def test_query_cap(self, fake_metrics, tmp_path):
        out = execute_numeric(
            [{"op": "value", "metric": "revenue"}] * 10,
            "AAPL", make_settings(tmp_path))
        assert len(out) == 6


class TestRouteQuestion:
    def test_valid_numeric_route(self, tmp_path):
        FakeAnthropic.queue = [tool_response({
            "route": "numeric", "reason": "figure",
            "queries": [{"op": "value", "metric": "revenue"}]})]
        r = route_question("revenue?", "AAPL", make_settings(tmp_path))
        assert r["route"] == "numeric"
        assert r["queries"][0]["metric"] == "revenue"

    def test_llm_failure_fails_open_to_narrative(self, tmp_path):
        FakeAnthropic.queue = []  # client will blow up
        r = route_question("q?", "AAPL", make_settings(tmp_path))
        assert r == {"route": "narrative", "queries": []}

    def test_numeric_with_no_queries_collapses_to_narrative(self, tmp_path):
        FakeAnthropic.queue = [tool_response({
            "route": "numeric", "reason": "x", "queries": []})]
        r = route_question("q?", "AAPL", make_settings(tmp_path))
        assert r["route"] == "narrative"
