"""Unit tests for the one-click digest orchestration (pipeline mocked)."""

from __future__ import annotations

import app.digest as digest_mod
from app.digest import DIGEST_SECTIONS, digest_summary, generate_digest
from tests.llm_fakes import make_settings


def fake_report(summary="section summary"):
    return {"summary": summary, "claims": [{"verdict": "SUPPORTED"}],
            "blocked_claims": [], "unsupported_rate": 0.0,
            "risk_flags": [], "unsupported_claims": []}


class TestGenerateDigest:
    def test_all_sections_and_figures_present(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr(
            digest_mod, "execute_numeric",
            lambda q, t, settings: [{"ok": True, "text": "AAPL revenue FY2025: $416.2B",
                                     "sources": [], "query": {}}])

        def fake_answer(question, ticker, settings, query_id):
            calls.append(query_id)
            return fake_report(f"answer to: {question[:30]}")

        monkeypatch.setattr(digest_mod, "answer_question", fake_answer)
        d = generate_digest("aapl", settings=make_settings(tmp_path))

        assert d["ticker"] == "AAPL"
        assert len(d["figures"]) == 1
        assert len(d["sections"]) == len(DIGEST_SECTIONS)
        assert all("report" in s for s in d["sections"])
        # section order preserved regardless of parallel completion order
        assert [s["key"] for s in d["sections"]] == [k for k, _, _ in DIGEST_SECTIONS]
        assert len(calls) == len(DIGEST_SECTIONS)

    def test_one_failed_section_fails_soft(self, monkeypatch, tmp_path):
        monkeypatch.setattr(digest_mod, "execute_numeric",
                            lambda q, t, settings: [])

        def flaky(question, ticker, settings, query_id):
            if "risk" in question.lower():
                raise RuntimeError("API down")
            return fake_report()

        monkeypatch.setattr(digest_mod, "answer_question", flaky)
        d = generate_digest("AAPL", settings=make_settings(tmp_path))

        failed = [s for s in d["sections"] if "error" in s]
        ok = [s for s in d["sections"] if "report" in s]
        assert len(failed) == 1 and "API down" in failed[0]["error"]
        assert len(ok) == len(DIGEST_SECTIONS) - 1

    def test_figures_failure_never_blocks(self, monkeypatch, tmp_path):
        def boom(q, t, settings):
            raise RuntimeError("no facts")

        monkeypatch.setattr(digest_mod, "execute_numeric", boom)
        monkeypatch.setattr(digest_mod, "answer_question",
                            lambda question, ticker, settings, query_id:
                            fake_report())
        d = generate_digest("AAPL", settings=make_settings(tmp_path))
        assert d["figures"] == []
        assert all("report" in s for s in d["sections"])


class TestDigestSummary:
    def test_compact_context_from_figures_and_sections(self):
        d = {"figures": [{"text": "AAPL revenue FY2025: $416.2B"}],
             "sections": [{"title": "Key risks",
                           "report": fake_report("competition is fierce")}]}
        s = digest_summary(d)
        assert "416.2B" in s and "Key risks: competition is fierce" in s
