"""Tests for the credibility-critical logic: verdict application, report
assembly (WARNING/ERROR rules), unsupported rate, and the numeric guard.

All pure functions — no DB, no LLM, no network.
"""

from __future__ import annotations

import pytest

from app.assembler import DISCLAIMER, assemble_report, unsupported_rate
from app.models import Chunk, Claim, Verdict
from app.pipeline import is_numeric_question
from app.verification import apply_verdicts, build_judge_input


def _claim(i: int, text: str = "claim text", cites: list[str] | None = None) -> Claim:
    return Claim(
        claim_id=f"q1_claim{i}",
        query_id="q1",
        text=text,
        cited_chunk_ids=cites if cites is not None else ["c1"],
    )


def _chunk(cid: str = "c1") -> Chunk:
    return Chunk(chunk_id=cid, doc_id="D_10K", ticker="AAPL",
                 text="Revenue grew due to Services.", section="Item 7")


class TestApplyVerdicts:
    def test_applies_matching_verdicts(self):
        claims = [_claim(1), _claim(2)]
        out = apply_verdicts(claims, [
            {"claim_id": "q1_claim1", "verdict": "SUPPORTED", "reason": "matches"},
            {"claim_id": "q1_claim2", "verdict": "CONTRADICTED", "reason": "conflict"},
        ])
        assert out[0].verdict is Verdict.SUPPORTED
        assert out[1].verdict is Verdict.CONTRADICTED
        assert out[1].judge_reason == "conflict"

    def test_missing_claim_defaults_to_not_enough_info(self):
        claims = [_claim(1)]
        apply_verdicts(claims, [])  # judge skipped it
        assert claims[0].verdict is Verdict.NOT_ENOUGH_INFO

    def test_invalid_verdict_string_fails_safe(self):
        claims = [_claim(1)]
        apply_verdicts(claims, [
            {"claim_id": "q1_claim1", "verdict": "TOTALLY_TRUE", "reason": "?"},
        ])
        assert claims[0].verdict is Verdict.NOT_ENOUGH_INFO

    def test_unknown_claim_ids_ignored(self):
        claims = [_claim(1)]
        apply_verdicts(claims, [
            {"claim_id": "q1_claim1", "verdict": "SUPPORTED", "reason": "ok"},
            {"claim_id": "hallucinated_id", "verdict": "SUPPORTED", "reason": "?"},
        ])
        assert claims[0].verdict is Verdict.SUPPORTED


class TestBuildJudgeInput:
    def test_batches_all_claims_into_one_prompt(self):
        claims = [_claim(1), _claim(2), _claim(3)]
        text = build_judge_input(claims, {"c1": _chunk()})
        # every claim appears once in the single payload — one batch call
        for c in claims:
            assert f'<claim id="{c.claim_id}">' in text

    def test_uncited_claim_marked_no_evidence(self):
        text = build_judge_input([_claim(1, cites=[])], {"c1": _chunk()})
        assert "(no evidence cited)" in text


class TestAssembleReport:
    def _judged_claims(self) -> list[Claim]:
        c1, c2, c3 = _claim(1, "supported"), _claim(2, "unverified"), _claim(3, "wrong")
        c1.verdict, c2.verdict, c3.verdict = (
            Verdict.SUPPORTED, Verdict.NOT_ENOUGH_INFO, Verdict.CONTRADICTED,
        )
        c2.judge_reason = "no evidence"
        c3.judge_reason = "conflicts with source"
        c3.kind = "risk"  # even a risk claim must be blocked when contradicted
        return [c1, c2, c3]

    def _report(self):
        return assemble_report("q1", "question?", "AAPL", "summary",
                               self._judged_claims(), [_chunk()])

    def test_contradicted_claim_is_blocked_not_displayed(self):
        report = self._report()
        displayed_ids = {c["claim_id"] for c in report["claims"]}
        assert "q1_claim3" not in displayed_ids
        assert report["blocked_claims"][0]["claim_id"] == "q1_claim3"
        # blocked claim must not leak into risk flags either
        assert report["risk_flags"] == []

    def test_warning_claim_shown_but_unverified(self):
        report = self._report()
        warn = next(c for c in report["claims"] if c["claim_id"] == "q1_claim2")
        assert warn["status"] == "WARNING"
        assert warn["unverified"] is True
        assert [u["claim_id"] for u in report["unsupported_claims"]] == ["q1_claim2"]

    def test_supported_claim_normal_with_citation(self):
        report = self._report()
        ok = next(c for c in report["claims"] if c["claim_id"] == "q1_claim1")
        assert ok["status"] == "OK"
        assert ok["unverified"] is False
        assert ok["citations"][0]["chunk_id"] == "c1"
        assert ok["credibility"] > 0.5

    def test_unsupported_rate_counts_nei_and_contradicted(self):
        report = self._report()
        assert report["unsupported_rate"] == pytest.approx(2 / 3)

    def test_disclaimer_always_present(self):
        assert self._report()["disclaimer"] == DISCLAIMER

    def test_unjudged_claim_fails_safe_to_warning(self):
        c = _claim(1)  # verdict is None
        report = assemble_report("q1", "?", "AAPL", "s", [c], [_chunk()])
        assert report["claims"][0]["status"] == "WARNING"


class TestUnsupportedRate:
    def test_empty_is_zero(self):
        assert unsupported_rate([]) == 0.0

    def test_all_supported_is_zero(self):
        c = _claim(1)
        c.verdict = Verdict.SUPPORTED
        assert unsupported_rate([c]) == 0.0


class TestNumericGuard:
    @pytest.mark.parametrize("q", [
        "苹果过去三年的营收 CAGR 是多少？",
        "帮我计算微软的平均毛利率",
        "特斯拉营收环比增长了百分之多少？",
        "What is the average revenue growth rate over 5 years?",
        "MSFT 和 AAPL 的营收合计是多少",
    ])
    def test_numeric_questions_rejected(self, q):
        assert is_numeric_question(q)

    @pytest.mark.parametrize("q", [
        "苹果最近一年的营收增长主要靠什么驱动？有没有风险因素？",
        "What drove revenue growth last year?",
        "微软在 AI 方面披露了哪些风险？",
        "特斯拉的主要业务分部有哪些？",
    ])
    def test_narrative_questions_pass(self, q):
        assert not is_numeric_question(q)
