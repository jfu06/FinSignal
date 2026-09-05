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
