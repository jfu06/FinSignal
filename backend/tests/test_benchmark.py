"""Unit tests for the FinanceBench benchmark pipeline (no network, no DB)."""

from __future__ import annotations

import json

import pytest

import eval.benchmark_data as bd
import eval.run_benchmark as rb
from eval.benchmark_data import BenchCase, _resolve_ciks, load_benchmark
from eval.run_benchmark import compose_model_answer, grade_answer, summarize
from tests.llm_fakes import FakeAnthropic, make_settings, tool_response


def case(**kw) -> BenchCase:
    base = dict(financebench_id="fb1", doc_name="3M_2018_10K", company="3M",
                question_type="metrics-generated", question="capex?",
                answer="$1577.00", justification="cash flow statement",
                evidence_texts=["..."])
    base.update(kw)
    return BenchCase(**base)


class TestResolveCiks:
    def test_cik_extracted_from_cloudfront_link(self):
        rows = [{"company": "Amazon", "doc_name": "AMZN_2017_10K",
                 "doc_link": "https://x.cloudfront.net/CIK-0001018724/a.pdf"}]
        assert _resolve_ciks(rows, {"Amazon"}) == {"Amazon": "0001018724"}

    def test_manual_map_wins_for_delisted_company(self, monkeypatch):
        monkeypatch.setattr(bd, "load_ticker_map", lambda: {})
        rows = [{"company": "Activision Blizzard", "doc_name": "d",
                 "doc_link": "https://investor.activision.com/x.pdf"}]
        out = _resolve_ciks(rows, {"Activision Blizzard"})
        assert out["Activision Blizzard"] == "0000718877"

    def test_name_prefix_match_against_registry(self, monkeypatch):
        monkeypatch.setattr(bd, "load_ticker_map", lambda: {
            "MMM": {"cik": "0000066740", "title": "3M CO"}})
        rows = [{"company": "3M", "doc_name": "d", "doc_link": ""}]
        assert _resolve_ciks(rows, {"3M"}) == {"3M": "0000066740"}

    def test_ambiguous_prefix_raises_with_guidance(self, monkeypatch):
        monkeypatch.setattr(bd, "load_ticker_map", lambda: {
            "AAA": {"cik": "1", "title": "ACME CO"},
            "BBB": {"cik": "2", "title": "ACME CONSOLIDATED INC"}})
        rows = [{"company": "Acme", "doc_name": "d", "doc_link": ""}]
        with pytest.raises(ValueError, match="_MANUAL_CIK"):
            _resolve_ciks(rows, {"Acme"})


class TestLoadBenchmark:
    def test_filters_to_10k_and_resolves(self, monkeypatch, tmp_path):
        docs_file = tmp_path / "docs.jsonl"
        qs_file = tmp_path / "qs.jsonl"
        docs_file.write_text("\n".join(json.dumps(d) for d in [
            {"doc_name": "AMZN_2017_10K", "company": "Amazon",
             "doc_type": "10k", "doc_period": 2017,
             "doc_link": "https://x/CIK-0001018724/a.pdf"},
            {"doc_name": "AMZN_Q1_2023_10Q", "company": "Amazon",
             "doc_type": "10q", "doc_period": 2023, "doc_link": ""},
        ]))
        qs_file.write_text("\n".join(json.dumps(q) for q in [
            {"financebench_id": "a", "doc_name": "AMZN_2017_10K",
             "company": "Amazon", "question_type": "novel-generated",
             "question": "q1", "answer": "42", "justification": "j",
             "evidence": [{"evidence_text": "e1"}]},
            {"financebench_id": "b", "doc_name": "AMZN_Q1_2023_10Q",
             "company": "Amazon", "question_type": "novel-generated",
             "question": "q2", "answer": "x", "justification": "",
             "evidence": []},
        ]))
        monkeypatch.setattr(bd, "QUESTIONS_FILE", qs_file)
        monkeypatch.setattr(bd, "DOCS_FILE", docs_file)
        docs, cases = load_benchmark()
        assert [d.doc_name for d in docs] == ["AMZN_2017_10K"]
        assert docs[0].cik == "0001018724" and docs[0].fy == 2017
        assert [c.financebench_id for c in cases] == ["a"]
        assert cases[0].evidence_texts == ["e1"]


class TestScoring:
    def test_compose_model_answer_includes_claims_and_numeric(self):
        report = {"summary": "S.",
                  "claims": [{"text": "c1", "unverified": False},
                             {"text": "c2", "unverified": True}],
                  "numeric": [{"text": "revenue FY2018: $32.8B"}]}
        text = compose_model_answer(report)
        assert "S." in text and "- c1" in text
        assert "c2 [unverified]" in text
        assert "$32.8B" in text

    def test_grade_answer_anthropic_path(self, tmp_path):
        FakeAnthropic.queue = [tool_response(
            {"grade": "correct", "reason": "matches"})]
        out = grade_answer(case(), "capex was $1,577 million",
                           make_settings(tmp_path))
        assert out == {"grade": "correct", "reason": "matches"}

    def test_invalid_grade_fails_safe_to_incorrect(self, tmp_path):
        FakeAnthropic.queue = [tool_response({"grade": "meh", "reason": ""})]
        assert grade_answer(case(), "x",
                            make_settings(tmp_path))["grade"] == "incorrect"

    def test_run_case_records_crash_after_retry(self, monkeypatch, tmp_path):
        calls = {"n": 0}

        def boom(*a, **k):  # noqa: ANN002, ANN003
            calls["n"] += 1
            raise RuntimeError("db down")

        monkeypatch.setattr(rb, "answer_question", boom)
        monkeypatch.setattr(rb.time, "sleep", lambda s: None)
        out = rb.run_case(case(), make_settings(tmp_path))
        assert out["grade"] == "crash" and "db down" in out["reason"]
        assert calls["n"] == 2  # one retry

    def test_run_case_passes_doc_id_and_grades(self, monkeypatch, tmp_path):
        seen = {}

        def fake_answer(question, ticker, settings=None, query_id=None,
                        doc_id=None, numeric_scope=None):
            seen.update(ticker=ticker, doc_id=doc_id)
            return {"summary": "capex $1,577M", "claims": [],
                    "unsupported_rate": 0.0, "latency_s": 1.0}

        monkeypatch.setattr(rb, "answer_question", fake_answer)
        monkeypatch.setattr(rb, "grade_answer",
                            lambda c, m, s: {"grade": "correct", "reason": "ok"})
        out = rb.run_case(case(), make_settings(tmp_path))
        assert seen == {"ticker": "3M", "doc_id": "3M_2018_10K"}
        assert out["grade"] == "correct"

    def test_summarize_math(self):
        results = [
            {"grade": "correct", "question_type": "novel-generated"},
            {"grade": "incorrect", "question_type": "novel-generated"},
            {"grade": "abstain", "question_type": "metrics-generated"},
            {"grade": "crash", "question_type": "metrics-generated"},
        ]
        s = summarize(results)
        assert s["accuracy"] == 0.25
        assert s["answered_accuracy"] == 0.5
        assert s["hallucination_rate"] == 0.25
        assert s["by_question_type"]["novel-generated"]["accuracy"] == 0.5
