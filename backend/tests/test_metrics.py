"""Unit tests for the metrics layer's pure selection/derivation logic."""

from __future__ import annotations

from datetime import date

import pytest

from app.metrics import (
    Fact,
    MetricError,
    fy_label,
    is_annual,
    is_quarterly,
    pick_annual,
    pick_instant,
    quarters_within,
)


def fact(start, end, val, accn="a1", filed="2026-01-01", form="10-K") -> Fact:
    return Fact(
        start=date.fromisoformat(start) if start else None,
        end=date.fromisoformat(end), val=val, accn=accn, form=form,
        filed=date.fromisoformat(filed),
    )


class TestPeriodClassification:
    def test_364_day_fiscal_year_is_annual(self):
        # 52-week fiscal calendars (§5.7) must count as annual
        assert is_annual(fact("2024-09-29", "2025-09-27", 1))

    def test_53_week_year_is_annual(self):
        assert is_annual(fact("2022-09-25", "2023-09-30", 1))

    def test_quarter_is_not_annual(self):
        f = fact("2025-06-29", "2025-09-27", 1)
        assert not is_annual(f) and is_quarterly(f)

    def test_instant_is_neither(self):
        f = fact(None, "2025-09-27", 1)
        assert not is_annual(f) and not is_quarterly(f)

    def test_fy_label_is_calendar_year_of_end(self):
        # AAPL FY2025 ends Sep 2025; an NVDA-style FY ends Jan of its label year
        assert fy_label(fact("2024-09-29", "2025-09-27", 1)) == 2025
        assert fy_label(fact("2025-01-27", "2026-01-25", 1)) == 2026


class TestPickAnnual:
    FACTS = [
        fact("2023-10-01", "2024-09-28", 391.0),
        fact("2024-09-29", "2025-09-27", 416.2),
        fact("2025-06-29", "2025-09-27", 102.5),   # a quarter — ignore
    ]

    def test_specific_fy(self):
        assert pick_annual(self.FACTS, 2024).val == 391.0

    def test_latest_when_fy_none(self):
        assert pick_annual(self.FACTS, None).val == 416.2

    def test_missing_fy_returns_none(self):
        assert pick_annual(self.FACTS, 2019) is None


class TestPickInstant:
    FACTS = [
        fact(None, "2025-06-28", 331.5),   # Q3 balance sheet
        fact(None, "2025-09-27", 344.1),   # FY-end balance sheet
        fact(None, "2024-09-28", 364.9),
    ]

    def test_anchored_to_fiscal_year_end(self):
        p = pick_instant(self.FACTS, anchor_end=date(2025, 9, 27), fy=2025)
        assert p.val == 344.1              # not the Q3 point

    def test_no_anchor_falls_back_to_latest_in_year(self):
        assert pick_instant(self.FACTS, None, fy=2024).val == 364.9

    def test_latest_overall_when_nothing_given(self):
        assert pick_instant(self.FACTS, None, None).val == 344.1


class TestQuartersWithin:
    def test_three_distinct_quarters_selected(self):
        period = (date(2024, 9, 29), date(2025, 9, 27))
        facts = [
            fact("2024-09-29", "2024-12-28", 124.3),
            fact("2024-12-29", "2025-03-29", 95.4),
            fact("2025-03-30", "2025-06-28", 94.0),
            # duplicate Q1 from a later comparative filing — must dedupe
            fact("2024-09-29", "2024-12-28", 124.3, accn="a2"),
            # annual point inside the range — not a quarter
            fact("2024-09-29", "2025-09-27", 416.2),
            # quarter from OUTSIDE the period
            fact("2023-12-31", "2024-03-30", 90.8),
        ]
        qs = quarters_within(facts, *period)
        assert [q.val for q in qs] == [124.3, 95.4, 94.0]

    def test_q4_math_from_selected_quarters(self):
        # FY 416.2 − (124.3 + 95.4 + 94.0) = 102.5
        assert round(416.2 - (124.3 + 95.4 + 94.0), 1) == 102.5


class TestErrors:
    def test_unknown_metric_raises(self):
        from app.metrics import get_metric
        from tests.llm_fakes import make_settings
        with pytest.raises(MetricError, match="Unknown metric"):
            get_metric("AAPL", "vibes_per_share", settings=make_settings(__import__("pathlib").Path("/tmp")))


class TestPublicApiWithFakeStore:
    """Exercise the public API over an in-memory fact store (no DB/network)."""

    STORE = {
        # priority-1 revenue tag only has recent years…
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"): [
            fact("2024-09-29", "2025-09-27", 416.2e9, accn="a25"),
            fact("2023-10-01", "2024-09-28", 391.0e9, accn="a24"),
            fact("2024-09-29", "2024-12-28", 124.3e9),
            fact("2024-12-29", "2025-03-29", 95.4e9),
            fact("2025-03-30", "2025-06-28", 94.0e9),
        ],
        # …older years live under the retired tag (§5.1 stitching)
        ("us-gaap", "SalesRevenueNet"): [
            fact("2017-10-01", "2018-09-29", 265.6e9, accn="a18"),
        ],
        ("us-gaap", "GrossProfit"): [
            fact("2024-09-29", "2025-09-27", 195.2e9),
        ],
        ("us-gaap", "Assets"): [
            fact(None, "2025-09-27", 344.1e9),
            fact(None, "2025-06-28", 331.5e9),
        ],
        ("us-gaap", "AssetsCurrent"): [
            fact(None, "2025-09-27", 152.0e9),
        ],
        ("us-gaap", "LiabilitiesCurrent"): [
            fact(None, "2025-09-27", 100.0e9),
        ],
        ("us-gaap", "InventoryNet"): [
            fact(None, "2025-09-27", 7.0e9),
        ],
    }

    @pytest.fixture(autouse=True)
    def _fake_store(self, monkeypatch):
        import app.metrics as metrics

        self.fetch_calls = []

        def fake_fetch(cik, taxonomy, tag, unit, settings, accn=None):
            self.fetch_calls.append({"cik": cik, "accn": accn})
            return self.STORE.get((taxonomy, tag), [])

        monkeypatch.setattr(metrics, "_fetch", fake_fetch)
        monkeypatch.setattr(metrics, "_cik", lambda t: 320193)

    def _s(self, tmp_path):
        from tests.llm_fakes import make_settings
        return make_settings(tmp_path)

    def test_get_metric_latest_and_specific_fy(self, tmp_path):
        from app.metrics import get_metric
        assert get_metric("AAPL", "revenue", settings=self._s(tmp_path)).value == 416.2e9
        assert get_metric("AAPL", "revenue", fy=2024,
                          settings=self._s(tmp_path)).value == 391.0e9

    def test_tag_fallback_reaches_retired_tag(self, tmp_path):
        from app.metrics import get_metric
        p = get_metric("AAPL", "revenue", fy=2018, settings=self._s(tmp_path))
        assert p.value == 265.6e9 and p.accn == "a18"

    def test_instant_metric_anchored_to_fy_end(self, tmp_path):
        from app.metrics import get_metric
        p = get_metric("AAPL", "total_assets", fy=2025,
                       settings=self._s(tmp_path))
        assert p.value == 344.1e9   # FY-end, not the Q3 balance sheet

    def test_yoy_growth_math(self, tmp_path):
        from app.metrics import yoy_growth
        g, cur, prev = yoy_growth("AAPL", "revenue", settings=self._s(tmp_path))
        assert g == pytest.approx((416.2 - 391.0) / 391.0)

    def test_ratio_uses_same_fy(self, tmp_path):
        from app.metrics import ratio
        r, num, den = ratio("AAPL", "gross_margin", settings=self._s(tmp_path))
        assert r == pytest.approx(195.2 / 416.2)
        assert num.fy == den.fy == 2025

    def test_q4_derivation(self, tmp_path):
        from app.metrics import q4_single_quarter
        p = q4_single_quarter("AAPL", "revenue", fy=2025,
                              settings=self._s(tmp_path))
        assert p.value == pytest.approx(416.2e9 - (124.3e9 + 95.4e9 + 94.0e9))
        assert p.derived is True

    def test_series_stitches_across_tags(self, tmp_path):
        from app.metrics import get_series
        pts = get_series("AAPL", "revenue", years=8, settings=self._s(tmp_path))
        fys = [p.fy for p in pts]
        assert 2025 in fys and 2024 in fys and 2018 in fys

    def test_compare_sorts_descending(self, tmp_path):
        from app.metrics import compare
        pts = compare(["AAPL", "AAPL"], "revenue", settings=self._s(tmp_path))
        assert [p.value for p in pts] == sorted(
            (p.value for p in pts), reverse=True)


class TestDerivedMetrics(TestPublicApiWithFakeStore):
    """DERIVED formulas over the same fake store."""

    def test_working_capital_is_subtraction(self, tmp_path):
        from app.metrics import derived_metric
        value, pts = derived_metric("AAPL", "working_capital",
                                    settings=self._s(tmp_path))
        assert value == 52.0e9
        assert [p.metric for p in pts] == ["current_assets",
                                           "current_liabilities"]

    def test_quick_ratio_excludes_inventory(self, tmp_path):
        from app.metrics import derived_metric
        value, _ = derived_metric("AAPL", "quick_ratio",
                                  settings=self._s(tmp_path))
        assert value == pytest.approx((152.0e9 - 7.0e9) / 100.0e9)

    def test_missing_component_returns_none(self, tmp_path, monkeypatch):
        import app.metrics as metrics
        from app.metrics import derived_metric
        monkeypatch.setitem(self.STORE, ("us-gaap", "InventoryNet"), [])
        try:
            assert derived_metric("AAPL", "quick_ratio",
                                  settings=self._s(tmp_path)) is None
        finally:
            pass  # monkeypatch restores the store entry

    def test_unknown_derived_raises(self, tmp_path):
        from app.metrics import MetricError, derived_metric
        with pytest.raises(MetricError, match="Unknown derived"):
            derived_metric("AAPL", "vibes", settings=self._s(tmp_path))

    def test_cik_override_skips_ticker_resolution(self, tmp_path, monkeypatch):
        import app.metrics as metrics
        from app.metrics import get_metric

        def explode(t):
            raise AssertionError("resolve_ticker must not be called")

        monkeypatch.setattr(metrics, "_cik", explode)
        p = get_metric("3M", "revenue", settings=self._s(tmp_path), cik=66740)
        assert p is not None
        assert self.fetch_calls[0]["cik"] == 66740

    def test_prev_year_component_resolves_fy_minus_one(self, tmp_path,
                                                       monkeypatch):
        import app.metrics as metrics
        from app.metrics import derived_metric
        calls = []
        real = metrics.get_metric

        def spy(t, m, fy=None, settings=None, cik=None, accn=None):
            calls.append((m, fy))
            # synthesize simple points so the DPO formula runs end to end
            from app.metrics import MetricPoint
            from datetime import date
            vals = {"accounts_payable": 30e9, "cost_of_revenue": 100e9,
                    "inventory": 12e9}
            base = vals[m] * (0.9 if fy == 2016 else 1.0)
            return MetricPoint(ticker=t, metric=m, fy=fy or 2017, value=base,
                               unit="USD", period_start=None,
                               period_end=date(2017, 12, 31), accn="a",
                               form="10-K", source_url="u")

        monkeypatch.setattr(metrics, "get_metric", spy)
        value, pts = derived_metric("AMZN", "days_payable_outstanding",
                                    fy=2017, settings=self._s(tmp_path))
        assert ("accounts_payable", 2017) in calls
        assert ("accounts_payable", 2016) in calls
        assert ("inventory", 2016) in calls
        # 365 * avg(30, 27) / (100 + 12 - 10.8) = 365*28.5/101.2
        assert value == pytest.approx(365 * 28.5e9 / 101.2e9)

    def test_accn_threaded_through_to_fetch(self, tmp_path):
        from app.metrics import get_metric
        get_metric("AAPL", "revenue", settings=self._s(tmp_path),
                   cik=320193, accn="a25")
        assert all(c["accn"] == "a25" for c in self.fetch_calls)


class TestFilingUrl:
    def test_falls_back_to_human_readable_index_page(self):
        from app.metrics import filing_url
        url = filing_url(320193, "0000320193-25-000079")
        assert url == ("https://www.sec.gov/Archives/edgar/data/320193/"
                       "000032019325000079/0000320193-25-000079-index.htm")

    def test_deep_links_ixbrl_viewer_when_primary_doc_known(self):
        from app.metrics import filing_url
        url = filing_url(320193, "0000320193-25-000079", "aapl-20250927.htm")
        assert url == ("https://www.sec.gov/ix?doc=/Archives/edgar/data/"
                       "320193/000032019325000079/aapl-20250927.htm")


class TestClassifyStatements:
    XML = """<FilingSummary><MyReports>
      <Report><ShortName>Cover Page</ShortName>
        <HtmlFileName>R1.htm</HtmlFileName></Report>
      <Report><ShortName>CONSOLIDATED STATEMENTS OF OPERATIONS</ShortName>
        <HtmlFileName>R3.htm</HtmlFileName></Report>
      <Report><ShortName>CONSOLIDATED STATEMENTS OF COMPREHENSIVE INCOME</ShortName>
        <HtmlFileName>R4.htm</HtmlFileName></Report>
      <Report><ShortName>CONSOLIDATED BALANCE SHEETS</ShortName>
        <HtmlFileName>R5.htm</HtmlFileName></Report>
      <Report><ShortName>CONSOLIDATED BALANCE SHEETS (Parenthetical)</ShortName>
        <HtmlFileName>R6.htm</HtmlFileName></Report>
      <Report><ShortName>CONSOLIDATED STATEMENTS OF CASH FLOWS</ShortName>
        <HtmlFileName>R8.htm</HtmlFileName></Report>
    </MyReports></FilingSummary>"""

    def test_maps_the_three_statements(self):
        from app.metrics import classify_statements
        out = classify_statements(self.XML)
        assert out == {"income": "R3.htm", "balance": "R5.htm",
                       "cashflow": "R8.htm"}

    def test_comprehensive_income_and_parenthetical_are_skipped(self):
        from app.metrics import classify_statements
        out = classify_statements(self.XML)
        assert out["income"] != "R4.htm" and out["balance"] != "R6.htm"

    def test_malformed_xml_yields_empty(self):
        from app.metrics import classify_statements
        assert classify_statements("<not-xml") == {}
