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
                        lambda t, m, fy=None, settings=None, **kw: point(metric=m))
    monkeypatch.setattr(router, "yoy_growth",
                        lambda t, m, fy=None, settings=None, **kw:
                        (0.064, point(metric=m), prev))
    monkeypatch.setattr(router, "cagr",
                        lambda t, m, years, settings=None, **kw:
                        (0.0181, point(metric=m), prev))
    monkeypatch.setattr(router, "ratio",
                        lambda t, name, fy=None, settings=None, **kw:
                        (0.469, point("gross_profit", 195.2e9), point()))
    monkeypatch.setattr(router, "q4_single_quarter",
                        lambda t, m, fy=None, settings=None, **kw: point(
                            metric=f"{m}_q4", value=102.5e9))
    monkeypatch.setattr(router, "get_series",
                        lambda t, m, years=5, settings=None, **kw:
                        [point(), prev])
    monkeypatch.setattr(router, "compare",
                        lambda ts, m, fy=None, settings=None, **kw:
                        [point(ticker="MSFT", value=35.6e9, metric=m),
                         point(ticker="AAPL", value=34.5e9, metric=m)])


class TestExecuteNumeric:
    def test_value_op_renders_with_source(self, fake_metrics, tmp_path):
        out = execute_numeric([{"op": "value", "metric": "revenue"}],
                              "AAPL", make_settings(tmp_path))
        assert out[0]["text"] == "AAPL revenue FY2025: $416.20B"
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
        assert "$102.50B" in out[0]["text"]

    def test_series_op(self, fake_metrics, tmp_path):
        out = execute_numeric([{"op": "series", "metric": "revenue"}],
                              "AAPL", make_settings(tmp_path))
        assert "FY2024 $391.00B; FY2025 $416.20B" in out[0]["text"]

    def test_average_op(self, fake_metrics, tmp_path):
        out = execute_numeric([{"op": "average", "metric": "revenue"}],
                              "AAPL", make_settings(tmp_path))
        assert "average" in out[0]["text"]
        assert "$403.60B" in out[0]["text"]  # (416.2+391.0)/2

    def test_compare_op_ranks(self, fake_metrics, tmp_path):
        out = execute_numeric(
            [{"op": "compare", "metric": "research_and_development",
              "tickers": ["AAPL", "MSFT"]}], "AAPL", make_settings(tmp_path))
        assert "MSFT FY2025 $35.60B; AAPL FY2025 $34.50B" in out[0]["text"]

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

    def test_derived_metric_op(self, fake_metrics, monkeypatch, tmp_path):
        monkeypatch.setattr(
            router, "derived_metric",
            lambda t, m, fy=None, settings=None, cik=None, accn=None:
            (1.45, [point("current_assets", 152.0e9),
                    point("current_liabilities", 100.0e9)]))
        out = execute_numeric([{"op": "value", "metric": "quick_ratio"}],
                              "AAPL", make_settings(tmp_path))
        assert "1.45x" in out[0]["text"]
        assert len(out[0]["sources"]) == 2

    def test_scope_threaded_to_metrics(self, monkeypatch, tmp_path):
        seen = {}

        def fake_get(t, m, fy=None, settings=None, cik=None, accn=None):
            seen.update(cik=cik, accn=accn)
            return point(metric=m)

        monkeypatch.setattr(router, "get_metric", fake_get)
        execute_numeric([{"op": "value", "metric": "revenue"}],
                        "3M", make_settings(tmp_path),
                        scope={"cik": 66740, "accn": "acc-18"})
        assert seen == {"cik": 66740, "accn": "acc-18"}

    def test_average_over_ratio_means_per_year_ratios(
            self, fake_metrics, monkeypatch, tmp_path):
        vals = {2025: 0.469, 2024: 0.451, 2023: 0.443}

        def fake_ratio(t, name, fy=None, settings=None, **kw):
            y = fy or 2025
            if y not in vals:
                return None
            return (vals[y], point("gross_profit", 195.2e9, fy=y),
                    point(fy=y))

        monkeypatch.setattr(router, "ratio", fake_ratio)
        out = execute_numeric(
            [{"op": "average", "metric": "gross_margin", "years": 3}],
            "AAPL", make_settings(tmp_path))
        assert "3-year average" in out[0]["text"]
        assert "45.4%" in out[0]["text"]  # mean(46.9, 45.1, 44.3)

    def test_series_over_ratio_lists_per_year_trend(
            self, fake_metrics, monkeypatch, tmp_path):
        vals = {2025: 0.469, 2024: 0.451, 2023: 0.443}

        def fake_ratio(t, name, fy=None, settings=None, **kw):
            y = fy or 2025
            if y not in vals:
                return None
            return (vals[y], point("gross_profit", 195.2e9, fy=y),
                    point(fy=y))

        monkeypatch.setattr(router, "ratio", fake_ratio)
        out = execute_numeric(
            [{"op": "series", "metric": "gross_margin", "years": 3}],
            "AAPL", make_settings(tmp_path))
        assert ("FY2023 44.3%; FY2024 45.1% (+0.8pp); FY2025 46.9% (+1.8pp)"
                in out[0]["text"])
        assert "+2.6pp over 2 years" in out[0]["text"]

    def test_query_cap_and_identical_results_collapse(
            self, fake_metrics, tmp_path):
        # at most 6 queries EXECUTE, and identical fact sets render ONE card
        out = execute_numeric(
            [{"op": "value", "metric": "revenue"}] * 10,
            "AAPL", make_settings(tmp_path))
        assert len(out) == 1


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
        assert r == {"route": "narrative", "queries": [], "companies": []}

    def test_numeric_with_no_queries_collapses_to_narrative(self, tmp_path):
        FakeAnthropic.queue = [tool_response({
            "route": "numeric", "reason": "x", "queries": []})]
        r = route_question("q?", "AAPL", make_settings(tmp_path))
        assert r["route"] == "narrative"


class TestSubsumption:
    def test_value_card_subsumed_by_yoy_card(self, fake_metrics, tmp_path):
        # review round 7 (SCHW): 'revenue FY2025: $23.92B' card fully
        # contained in the '+22.0% YoY' card — one card, not two
        out = execute_numeric(
            [{"op": "value", "metric": "revenue"},
             {"op": "yoy", "metric": "revenue"}],
            "AAPL", make_settings(tmp_path))
        assert len(out) == 1
        assert "YoY" in out[0]["text"]


class TestSeriesSynthesis:
    def test_two_point_series_gets_growth(self, fake_metrics, tmp_path):
        out = execute_numeric(
            [{"op": "series", "metric": "revenue", "years": 3}],
            "AAPL", make_settings(tmp_path))
        assert "(+6.4%)" in out[0]["text"]  # per-year growth attached

    def test_synthesis_growth_cagr_and_acceleration(self):
        # review round 7 (SCHW): 18.84 -> 19.61 -> 23.92 answered as raw
        # points; the flat-flat-jump shape IS the information
        pts = [point(fy=2023, value=18.84e9), point(fy=2024, value=19.61e9),
               point(fy=2025, value=23.92e9)]
        text = router._series_text("SCHW", "revenue", pts)
        assert "(+4.1%)" in text and "(+22.0%)" in text
        assert "2-yr CAGR +12.7%" in text
        assert "growth accelerated in FY2025" in text

    def test_synthesis_flags_decline_years(self):
        pts = [point(fy=2023, value=100.0e9), point(fy=2024, value=90.0e9),
               point(fy=2025, value=95.0e9)]
        text = router._series_text("T", "revenue", pts)
        assert "declined in FY2024" in text
