"""Unit tests for the self-supervised smoke eval (LLM + DB mocked)."""

from __future__ import annotations

import json

import pytest

import app.smoke_eval as smoke_eval
from app.models import Chunk
from app.smoke_eval import generate_questions, run_smoke_eval, score_results
from tests.llm_fakes import FakeAnthropic, make_settings, tool_response


def chunk(cid: str) -> Chunk:
    return Chunk(chunk_id=cid, doc_id="D", ticker="NVDA",
                 text="Data Center revenue was $193,737 million.", section="Item 7")


class TestGenerateQuestions:
    def test_filters_hallucinated_ids_and_numeric_questions(self, tmp_path):
        FakeAnthropic.queue = [tool_response({"items": [
            {"chunk_id": "c1", "question": "What drove Data Center revenue?"},
            {"chunk_id": "made_up", "question": "What is disclosed?"},
            {"chunk_id": "c2", "question": "What is the average growth rate?"},
        ]})]
        out = generate_questions([chunk("c1"), chunk("c2")],
                                 make_settings(tmp_path))
        assert out == [{"chunk_id": "c1",
                        "question": "What drove Data Center revenue?"}]


class TestScoreResults:
    def _ok(self, hit=True, n_claims=5, n_unsupported=0):
        return {"retrieval_hit": hit, "n_claims": n_claims,
                "n_unsupported": n_unsupported}

    def test_all_good_passes(self, tmp_path):
        s = score_results([self._ok() for _ in range(5)], make_settings(tmp_path))
        assert s["passed"] is True
        assert s["retrieval_hit_rate"] == 1.0

    def test_low_hit_rate_is_diagnostic_not_failing(self, tmp_path):
        # MMR diversification legitimately lowers source-paragraph hit rate
        # while answers still verify — hit_rate is reported, never failed on
        results = [self._ok(hit=i < 1) for i in range(5)]  # 1/5 = 0.2
        out = score_results(results, make_settings(tmp_path))
        assert out["passed"] is True
        assert out["retrieval_hit_rate"] == 0.2

    def test_unsupported_claims_fail(self, tmp_path):
        results = [self._ok() for _ in range(4)] + [self._ok(n_unsupported=2)]
        s = score_results(results, make_settings(tmp_path))
        assert s["unsupported_rate"] == pytest.approx(2 / 25)
        assert s["passed"] is False

    def test_crashed_question_fails(self, tmp_path):
        results = [self._ok() for _ in range(4)] + [{"error": "boom"}]
        assert score_results(results, make_settings(tmp_path))["passed"] is False

    def test_empty_results_fail(self, tmp_path):
        assert score_results([], make_settings(tmp_path))["passed"] is False

    def test_skipped_questions_excluded_not_failed(self, tmp_path):
        results = [self._ok() for _ in range(4)] + [{"skipped": True}]
        s = score_results(results, make_settings(tmp_path))
        assert s["n_crashed"] == 0
        assert s["retrieval_hit_rate"] == 1.0  # 4/4 scored, skip excluded
        assert s["passed"] is True


class TestEvidenceHit:
    def test_near_duplicate_chunk_counts(self):
        from app.smoke_eval import evidence_hit
        src = ("Cash equivalents and marketable equity securities are measured "
               "at fair value within the fair value hierarchy using quoted prices.")
        neighbor = ("The following table shows cash equivalents and marketable "
                    "equity securities measured at fair value using quoted "
                    "prices within the hierarchy as of December 31.")
        assert evidence_hit(src, ["totally unrelated text", neighbor])

    def test_unrelated_chunks_do_not_count(self):
        from app.smoke_eval import evidence_hit
        assert not evidence_hit(
            "Cash equivalents measured at fair value hierarchy quoted prices",
            ["Automotive regulatory credits boosted vehicle deliveries."],
        )


class TestRunSmokeEval:
    def test_end_to_end_with_mocks(self, monkeypatch, tmp_path):
        monkeypatch.setattr(smoke_eval, "DATA_RAW", tmp_path)
        monkeypatch.setattr(smoke_eval, "sample_chunks",
                            lambda t, n, s: [chunk("c1"), chunk("c2")])
        monkeypatch.setattr(smoke_eval, "generate_questions", lambda c, s: [
            {"chunk_id": "c1", "question": "Q1?"},
            {"chunk_id": "c2", "question": "Q2?"},
        ])

        def fake_answer(question, ticker, settings, query_id):
            src = "c1" if question == "Q1?" else "c2"
            return {"claims": [{"verdict": "SUPPORTED"}], "blocked_claims": [],
                    "retrieved_chunk_ids": [src, "other"]}

        monkeypatch.setattr(smoke_eval, "answer_question", fake_answer)
        monkeypatch.setattr(smoke_eval, "numeric_probes",
                            lambda t_, s_: {"numeric_ok": True,
                                            "numeric_notes": []})

        record = run_smoke_eval("nvda", settings=make_settings(tmp_path))

        assert record["passed"] is True
        assert record["retrieval_hits"] == 2
        # persisted for the UI
        saved = json.loads((tmp_path / "NVDA_smoke.json").read_text())
        assert saved["passed"] is True
        assert smoke_eval.load_smoke_result("NVDA")["passed"] is True

    def test_no_corpus_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(smoke_eval, "sample_chunks", lambda t, n, s: [])
        with pytest.raises(ValueError, match="No corpus"):
            run_smoke_eval("ZZZZ", settings=make_settings(tmp_path))


class TestNumericProbes:
    def test_scale_pollution_flagged(self, monkeypatch, tmp_path):
        from app import smoke_eval
        from types import SimpleNamespace as NS

        def fake_metric(ticker, metric, settings=None, fy=None):
            if metric == "revenue":
                return NS(value=23.9e9, fy=2025)
            return NS(value=8.85e6, fy=2025)  # 1000x mis-scaled net income

        import app.metrics as metrics
        monkeypatch.setattr(metrics, "get_metric", fake_metric)
        out = smoke_eval.numeric_probes("SCHW", make_settings(tmp_path))
        assert out["numeric_ok"] is False
        assert any("scale pollution" in n for n in out["numeric_notes"])

    def test_healthy_figures_pass(self, monkeypatch, tmp_path):
        from app import smoke_eval
        from types import SimpleNamespace as NS
        import app.metrics as metrics
        monkeypatch.setattr(
            metrics, "get_metric",
            lambda t, m, settings=None, fy=None: NS(
                value=23.9e9 if m == "revenue" else 8.85e9, fy=2025))
        out = smoke_eval.numeric_probes("SCHW", make_settings(tmp_path))
        assert out["numeric_ok"] is True and out["numeric_notes"] == []

    def test_missing_facts_flagged(self, monkeypatch, tmp_path):
        from app import smoke_eval
        import app.metrics as metrics
        monkeypatch.setattr(metrics, "get_metric",
                            lambda t, m, settings=None, fy=None: None)
        out = smoke_eval.numeric_probes("T", make_settings(tmp_path))
        assert out["numeric_ok"] is False
