"""Unit tests for the numeric-correctness golden check (pure)."""

from __future__ import annotations

from eval.run_eval import check_numeric_case

CASE = {"expected_substrings": ["416.2"], "expected_accn": "0000320193-25-000079"}
GOOD_REPORT = {"numeric": [{
    "text": "AAPL revenue FY2025: $416.2B",
    "sources": [{"accn": "0000320193-25-000079", "form": "10-K", "url": "u"}],
}]}


class TestCheckNumericCase:
    def test_correct_value_and_accn_passes(self):
        assert check_numeric_case(CASE, GOOD_REPORT)["ok"] is True

    def test_wrong_value_fails(self):
        bad = {"numeric": [{"text": "AAPL revenue FY2025: $999.9B",
                            "sources": GOOD_REPORT["numeric"][0]["sources"]}]}
        r = check_numeric_case(CASE, bad)
        assert r["ok"] is False and r["values_ok"] is False

    def test_wrong_provenance_fails(self):
        bad = {"numeric": [{"text": "AAPL revenue FY2025: $416.2B",
                            "sources": [{"accn": "other"}]}]}
        r = check_numeric_case(CASE, bad)
        assert r["ok"] is False and r["accn_ok"] is False

    def test_no_numeric_answer_fails(self):
        r = check_numeric_case(CASE, {"numeric": [], "claims": [{"x": 1}]})
        assert r["ok"] is False and r["numeric_answered"] is False

    def test_accn_optional(self):
        case = {"expected_substrings": ["416.2"], "expected_accn": None}
        assert check_numeric_case(case, GOOD_REPORT)["ok"] is True
