"""Unit tests for the deterministic span-overlap checks (pure functions)."""

from __future__ import annotations

import pytest

from app.span_overlap import check_claim, number_match, token_overlap

EVIDENCE = (
    "Total net sales were $416,161 million in 2025 compared to $391,035 "
    "million in 2024. Services net sales increased to $109,158 million, "
    "driven by advertising and cloud services revenue growth."
)


class TestNumberMatch:
    def test_exact_figures_match(self):
        assert number_match("Net sales were $416,161 million.", EVIDENCE) == 1.0

    def test_cross_format_chinese_units_match(self):
        # 4,161.61亿美元 -> digits "416161" == evidence "$416,161 (million)"
        claim = "苹果2025财年总净销售额为4,161.61亿美元，高于2024财年的3,910.35亿美元。"
        assert number_match(claim, EVIDENCE) == 1.0

    def test_fabricated_number_fails(self):
        claim = "Net sales were $999,999 million."
        assert number_match(claim, EVIDENCE) == 0.0

    def test_partial_match_is_fractional(self):
        claim = "Sales were $416,161 million, with $123,456 million from iPhone."
        assert number_match(claim, EVIDENCE) == pytest.approx(0.5)

    def test_no_numbers_returns_none(self):
        assert number_match("Revenue grew due to Services.", EVIDENCE) is None

    def test_small_numbers_ignored(self):
        # "52" (weeks) has < 3 digits — not worth flagging
        assert number_match("The fiscal year had 52 weeks.", EVIDENCE) is None


class TestTokenOverlap:
    def test_english_claim_high_overlap(self):
        claim = "Services net sales increased, driven by advertising and cloud revenue."
        assert token_overlap(claim, EVIDENCE) >= 0.7

    def test_english_claim_off_topic_low_overlap(self):
        claim = "Automotive regulatory credits boosted vehicle deliveries substantially."
        assert token_overlap(claim, EVIDENCE) <= 0.3

    def test_chinese_claim_is_na(self):
        claim = "服务业务收入增长明显。"
        assert token_overlap(claim, EVIDENCE) is None


class TestCheckClaim:
    def test_supported_claim_not_flagged(self):
        result = check_claim("Net sales were $416,161 million.", [EVIDENCE])
        assert result["number_match"] == 1.0
        assert result["flagged"] is False

    def test_fabricated_number_flagged(self):
        result = check_claim("Net sales were $999,999 million.", [EVIDENCE])
        assert result["flagged"] is True

    def test_no_numbers_never_flagged(self):
        result = check_claim("收入增长主要来自服务业务。", [EVIDENCE])
        assert result["number_match"] is None
        assert result["flagged"] is False

    def test_evidence_combined_across_chunks(self):
        result = check_claim(
            "Sales were $416,161 million and R&D spend was $31,370 million.",
            [EVIDENCE, "Research and development expense was $31,370 million."],
        )
        assert result["number_match"] == 1.0


class TestUnitNormalization:
    """The reviewer's false-positive: correctly-converted figures were
    flagged 'not found verbatim' because matching was digit-string based."""

    EV = ("Total net sales 416,161 391,035 $383,285  "
          "Services (1) 109,158 96,169 85,200")

    def test_billions_claim_matches_millions_table(self):
        claim = ("Apple's total net sales rose to $416.2 billion in fiscal "
                 "2025 from $391.0 billion in fiscal 2024.")
        assert number_match(claim, self.EV) == 1.0

    def test_years_are_not_financial_figures(self):
        # "fiscal 2025" must not count as an unmatched number
        assert number_match("Sales grew in fiscal 2025.", self.EV) is None

    def test_fabricated_billions_still_fail(self):
        assert number_match("Net sales were $420 billion.", self.EV) == 0.0

    def test_chinese_scale_words(self):
        assert number_match("总净销售额为4,161.61亿美元。", self.EV) == 1.0
