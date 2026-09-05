"""Mock-based unit tests for generation, batch verification, and pipeline
orchestration — no network, no DB, no real LLM.

These cover the critical control flow: structured-output validation, the
citation whitelist, the single-batch judge call contract, input validation,
the numeric-question boundary, and the one-shot regeneration path.
"""

from __future__ import annotations

import pytest

import app.generation as generation
import app.pipeline as pipeline
import app.verification as verification
from app.embeddings import _prefixes
from app.ingest import _doc_id
from app.models import Claim, Verdict
from app.refinement import RefinementTrace
from tests.llm_fakes import FakeAnthropic, make_chunk, make_settings, tool_response


class TestGenerateAnswer:
    def test_citation_whitelist_strips_unknown_ids(self, tmp_path):
        FakeAnthropic.queue = [tool_response({
            "summary": "s",
            "claims": [{"text": "t", "cited_chunk_ids": ["c1", "made_up"],
                        "kind": "insight"}],
        })]
        _, claims = generation.generate_answer(
            "q1", "question", [make_chunk()], settings=make_settings(tmp_path))
        assert claims[0].cited_chunk_ids == ["c1"]

    def test_empty_claims_list_is_valid(self, tmp_path):
        FakeAnthropic.queue = [tool_response({"summary": "nothing found", "claims": []})]
        summary, claims = generation.generate_answer(
            "q1", "question", [make_chunk()], settings=make_settings(tmp_path))
        assert claims == [] and summary == "nothing found"

    def test_truncated_output_retried_once(self, tmp_path):
        good = {"summary": "s",
                "claims": [{"text": "t", "cited_chunk_ids": ["c1"], "kind": "insight"}]}
        FakeAnthropic.queue = [tool_response(good, stop_reason="max_tokens"),
                               tool_response(good)]
        _, claims = generation.generate_answer(
            "q1", "question", [make_chunk()], settings=make_settings(tmp_path))
        assert len(claims) == 1
        assert len(FakeAnthropic.calls) == 2
        # the retry must carry the explicit format correction
        retry_prompt = FakeAnthropic.calls[1]["messages"][0]["content"]
        assert "previous record_answer call was malformed" in retry_prompt
        assert "malformed" not in FakeAnthropic.calls[0]["messages"][0]["content"]

    def test_flattened_single_claim_salvaged(self, tmp_path):
        # Observed failure mode: model flattens ONE claim to the top level.
        FakeAnthropic.queue = [tool_response({
            "summary": "s",
            "claims": "Total marketable securities were $100,000 million.",
            "cited_chunk_ids": ["c1"],
            "kind": "insight",
        })]
        _, claims = generation.generate_answer(
            "q1", "question", [make_chunk()], settings=make_settings(tmp_path))
        assert len(claims) == 1
        assert claims[0].cited_chunk_ids == ["c1"]
        assert len(FakeAnthropic.calls) == 1  # salvaged, no retry burned

    def test_single_key_envelope_unwrapped(self, tmp_path):
        # Observed: model wraps the whole payload in {"parameter": {...}}.
        FakeAnthropic.queue = [tool_response({"parameter": {
            "summary": "s",
            "claims": [{"text": "t", "cited_chunk_ids": ["c1"], "kind": "insight"}],
        }})]
        _, claims = generation.generate_answer(
            "q1", "question", [make_chunk()], settings=make_settings(tmp_path))
        assert len(claims) == 1 and claims[0].cited_chunk_ids == ["c1"]

    def test_envelope_with_json_string_payload(self, tmp_path):
        import json as _json
        payload = _json.dumps({"summary": "s", "claims": [
            {"text": "t", "cited_chunk_ids": ["c1"], "kind": "insight"}]})
        FakeAnthropic.queue = [tool_response({"input": payload})]
        _, claims = generation.generate_answer(
            "q1", "question", [make_chunk()], settings=make_settings(tmp_path))
        assert len(claims) == 1

    def test_claims_as_single_dict_wrapped(self, tmp_path):
        FakeAnthropic.queue = [tool_response({
            "summary": "s",
            "claims": {"text": "t", "cited_chunk_ids": ["c1"], "kind": "risk"},
        })]
        _, claims = generation.generate_answer(
            "q1", "question", [make_chunk()], settings=make_settings(tmp_path))
        assert len(claims) == 1 and claims[0].kind == "risk"

    def test_bare_string_claim_items_become_citeless_claims(self, tmp_path):
        FakeAnthropic.queue = [tool_response({
            "summary": "s", "claims": ["a bare string claim"],
        })]
        _, claims = generation.generate_answer(
            "q1", "question", [make_chunk()], settings=make_settings(tmp_path))
        assert claims[0].cited_chunk_ids == []  # judge will flag it — fail-safe

    def test_missing_summary_retried_then_fallback(self, tmp_path):
        no_summary = {"claims": [{"text": "t", "cited_chunk_ids": ["c1"],
                                  "kind": "insight"}]}
        FakeAnthropic.queue = [tool_response(no_summary), tool_response(no_summary)]
        summary, claims = generation.generate_answer(
            "q1", "question", [make_chunk()], settings=make_settings(tmp_path))
        assert len(FakeAnthropic.calls) == 2   # one retry for the summary
        assert len(claims) == 1                # second attempt accepted anyway
        assert summary                          # fallback text, never empty

    def test_invalid_twice_raises(self, tmp_path):
        bad = tool_response({"summary": "s"})  # claims key missing entirely
        FakeAnthropic.queue = [bad, tool_response({"summary": "s"})]
        with pytest.raises(RuntimeError, match="invalid structured output"):
            generation.generate_answer(
                "q1", "question", [make_chunk()], settings=make_settings(tmp_path))

    def test_regeneration_feedback_included_in_prompt(self, tmp_path):
        FakeAnthropic.queue = [tool_response({"summary": "s", "claims": []})]
        generation.generate_answer(
            "q1", "question", [make_chunk()],
            settings=make_settings(tmp_path), feedback="claim X was wrong")
        prompt = FakeAnthropic.calls[0]["messages"][0]["content"]
        assert "claim X was wrong" in prompt


class TestVerifyClaimsBatch:
    def test_single_call_for_all_claims(self, tmp_path):
        claims = [
            Claim(claim_id=f"q1_claim{i}", query_id="q1", text=f"t{i}",
                  cited_chunk_ids=["c1"])
            for i in range(1, 4)
        ]
        FakeAnthropic.queue = [tool_response({"verdicts": [
            {"claim_id": "q1_claim1", "verdict": "SUPPORTED", "reason": "ok"},
            {"claim_id": "q1_claim2", "verdict": "NOT_ENOUGH_INFO", "reason": "thin"},
            {"claim_id": "q1_claim3", "verdict": "CONTRADICTED", "reason": "wrong"},
        ]})]
        out = verification.verify_claims_batch(
            claims, {"c1": make_chunk()}, settings=make_settings(tmp_path))
        # THE contract: one batch call, never one per claim
        assert len(FakeAnthropic.calls) == 1
        assert [c.verdict for c in out] == [
            Verdict.SUPPORTED, Verdict.NOT_ENOUGH_INFO, Verdict.CONTRADICTED]

    def test_empty_claims_no_llm_call(self, tmp_path):
        assert verification.verify_claims_batch(
            [], {}, settings=make_settings(tmp_path)) == []
        assert FakeAnthropic.calls == []


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Wire pipeline internals to fakes; returns a mutable test harness."""

    state = {"generate_calls": 0, "verify_batches": [], "persisted": []}
    chunk = make_chunk()

    monkeypatch.setattr(pipeline, "_known_tickers", lambda s: {"AAPL"})
    def fake_retrieve(q, t, settings, query_id, doc_id=None, coverage=False):
        state["retrieve_doc_id"] = doc_id
        return [chunk], RefinementTrace(rounds=1, final_query=q, final_k=6)

    monkeypatch.setattr(pipeline, "retrieve_refined", fake_retrieve)
    # default routing: narrative, no numeric queries; tests override state
    state["route"] = {"route": "narrative", "queries": [],
                      "companies": []}

    def fake_route(q, t, settings, query_id):
        state["route_calls"] = state.get("route_calls", 0) + 1
        return state["route"]

    monkeypatch.setattr(pipeline, "route_question", fake_route)
    # SEC registry mock: MSFT/NVDA are real listings, others are not
    def fake_resolve(tk):
        if tk in ("MSFT", "NVDA"):
            return {"cik": "1", "title": tk}
        raise pipeline.EdgarError("unknown")
    monkeypatch.setattr(pipeline, "resolve_ticker", fake_resolve)
    monkeypatch.setattr(
        pipeline, "execute_numeric",
        lambda queries, t, settings, scope=None: state.get("numeric", []))
    monkeypatch.setattr(pipeline, "_persist_claims",
                        lambda claims, s: state["persisted"].append(list(claims)))

    def fake_generate(query_id, question, chunks, settings, feedback=None):
        state["generate_calls"] += 1
        claim = Claim(claim_id=f"{query_id}_claim1", query_id=query_id,
                      text="t", cited_chunk_ids=["c1"])
        return "summary", [claim]

    # first batch: CONTRADICTED (forces regen); second batch: SUPPORTED
    verdict_script = [Verdict.CONTRADICTED, Verdict.SUPPORTED]

    def fake_verify(claims, chunks_by_id, settings):
        verdict = verdict_script[len(state["verify_batches"])]
        for c in claims:
            c.verdict, c.judge_reason = verdict, "scripted"
        state["verify_batches"].append([c.claim_id for c in claims])
        return claims

    monkeypatch.setattr(pipeline, "generate_answer", fake_generate)
    monkeypatch.setattr(pipeline, "verify_claims_batch", fake_verify)
    return state, make_settings(tmp_path)


class TestAnswerQuestionOrchestration:
    def test_contradicted_triggers_exactly_one_regeneration(self, wired):
        state, settings = wired
        report = pipeline.answer_question("why?", "AAPL", settings=settings,
                                          query_id="q1")
        assert state["generate_calls"] == 2          # original + ONE regen
        assert len(state["verify_batches"]) == 2     # re-verified once
        assert report["unsupported_rate"] == 0.0     # final attempt clean
        assert report["blocked_claims"] == []
        # failed attempt was persisted for the record, then the final one
        assert len(state["persisted"]) == 2

    def test_unknown_ticker_rejected(self, wired):
        _, settings = wired
        with pytest.raises(pipeline.PipelineError, match="Unknown ticker"):
            pipeline.answer_question("why?", "NVDA", settings=settings)

    def test_empty_question_rejected(self, wired):
        _, settings = wired
        with pytest.raises(pipeline.PipelineError, match="non-empty"):
            pipeline.answer_question("   ", "AAPL", settings=settings)

    def test_numeric_route_answers_without_rag(self, wired):
        state, settings = wired
        state["route"] = {"route": "numeric",
                          "queries": [{"op": "cagr", "metric": "revenue"}]}
        state["numeric"] = [{"ok": True, "text": "AAPL revenue 3Y CAGR: +1.81%",
                             "sources": [], "query": {}}]
        report = pipeline.answer_question("苹果的营收 CAGR 是多少？", "AAPL",
                                          settings=settings)
        assert report["numeric"][0]["text"].startswith("AAPL revenue")
        assert "CAGR" in report["summary"]
        assert state["generate_calls"] == 0          # RAG never touched
        assert report["unsupported_rate"] == 0.0

    def test_numeric_route_with_no_results_falls_back_to_rag(self, wired):
        state, settings = wired
        state["route"] = {"route": "numeric",
                          "queries": [{"op": "value", "metric": "revenue"}]}
        state["numeric"] = []                        # execution came up empty
        report = pipeline.answer_question("some numeric question", "AAPL",
                                          settings=settings)
        assert state["generate_calls"] >= 1          # fell back to RAG
        assert report["claims"]

    def test_hybrid_route_merges_numeric_into_rag_report(self, wired):
        state, settings = wired
        state["route"] = {"route": "hybrid",
                          "queries": [{"op": "yoy", "metric": "revenue"}]}
        state["numeric"] = [{"ok": True, "text": "AAPL revenue +6.4% YoY",
                             "sources": [], "query": {}}]
        report = pipeline.answer_question("营收增长多少?靠什么驱动?", "AAPL",
                                          settings=settings)
        assert state["generate_calls"] >= 1          # RAG ran
        assert report["numeric"][0]["text"] == "AAPL revenue +6.4% YoY"
        assert report["claims"]                      # narrative too


class TestOracleDocumentMode:
    """doc_id pins retrieval and bypasses routing/ticker checks (FinanceBench)."""

    def test_doc_id_skips_ticker_validation_and_router(self, wired):
        state, settings = wired
        report = pipeline.answer_question(
            "capex?", "3M", settings=settings, doc_id="3M_2018_10K")
        # "3M" is not in the live corpus ({"AAPL"}) — no PipelineError raised
        assert report["claims"]
        assert state["retrieve_doc_id"] == "3M_2018_10K"
        assert state.get("route_calls", 0) == 0  # router bypassed entirely

    def test_doc_id_with_numeric_scope_runs_router(self, wired):
        state, settings = wired
        state["route"] = {"route": "numeric", "companies": [],
                          "queries": [{"op": "value", "metric": "revenue"}]}
        state["numeric"] = [{"ok": True, "text": "3M revenue FY2018: $32.8B",
                             "sources": []}]
        report = pipeline.answer_question(
            "FY2018 revenue?", "3M", settings=settings,
            doc_id="3M_2018_10K",
            numeric_scope={"cik": 66740, "accn": "acc-18"})
        assert state["route_calls"] == 1          # router DID run
        assert report["numeric"][0]["text"].startswith("3M revenue")
        assert report["claims"] == []             # pure numeric, no RAG

    def test_without_doc_id_router_still_runs(self, wired):
        state, settings = wired
        pipeline.answer_question("q?", "AAPL", settings=settings)
        assert state["route_calls"] == 1
        assert state["retrieve_doc_id"] is None


class TestCompanyDetection:
    def test_mentioned_corpus_company_overrides_default(self, wired, monkeypatch):
        state, settings = wired
        import app.pipeline as pipeline
        monkeypatch.setattr(pipeline, "_known_tickers",
                            lambda s: {"AAPL", "MSFT"})
        state["route"] = {"route": "narrative", "queries": [],
                          "companies": ["MSFT"]}
        report = pipeline.answer_question("How does Microsoft make money?",
                                          "AAPL", settings=settings)
        assert report["ticker"] == "MSFT"      # switched off the default
        assert report["claims"]

    def test_default_kept_when_mentioned_alongside_others(self, wired, monkeypatch):
        state, settings = wired
        import app.pipeline as pipeline
        monkeypatch.setattr(pipeline, "_known_tickers",
                            lambda s: {"AAPL", "MSFT"})
        state["route"] = {"route": "narrative", "queries": [],
                          "companies": ["AAPL", "MSFT"]}
        report = pipeline.answer_question("Compare Apple and Microsoft",
                                          "AAPL", settings=settings)
        assert report["ticker"] == "AAPL"      # default is among mentions

    def test_valid_but_unonboarded_company_asks_for_onboarding(self, wired):
        state, settings = wired
        state["route"] = {"route": "narrative", "queries": [],
                          "companies": ["NVDA"]}
        report = pipeline.answer_question("What does Nvidia do?",
                                          "AAPL", settings=settings)
        assert report["needs_onboarding"] == "NVDA"
        assert state["generate_calls"] == 0    # pipeline stopped early

    def test_hallucinated_symbol_falls_back_to_default(self, wired):
        state, settings = wired
        state["route"] = {"route": "narrative", "queries": [],
                          "companies": ["ZZZZ"]}
        report = pipeline.answer_question("What does Zorbcorp do?",
                                          "AAPL", settings=settings)
        assert report["ticker"] == "AAPL"      # kept the default, answered
        assert report["claims"]


class TestGraphStructure:
    def test_graph_has_all_pipeline_nodes_and_regen_loop(self, tmp_path):
        compiled = pipeline.build_graph(make_settings(tmp_path))
        g = compiled.get_graph()
        nodes = set(g.nodes)
        assert {"route_question", "run_numeric", "assemble_numeric",
                "retrieve", "generate", "verify",
                "flag_regeneration", "assemble"} <= nodes
        edges = {(e.source, e.target) for e in g.edges}
        # the one-shot regeneration loop is an explicit edge back to generate
        assert ("flag_regeneration", "generate") in edges
        # the numeric fallback path re-enters the RAG line
        assert ("run_numeric", "retrieve") in edges


class TestSmallHelpers:
    def test_e5_prefixes(self):
        assert _prefixes("intfloat/multilingual-e5-small") == ("query: ", "passage: ")

    def test_bge_prefixes(self):
        q, p = _prefixes("BAAI/bge-small-en-v1.5")
        assert q.startswith("Represent this sentence") and p == ""

    def test_unknown_model_no_prefixes(self):
        assert _prefixes("some/other-model") == ("", "")

    def test_doc_id_with_meta(self, tmp_path):
        meta = tmp_path / "AAPL_10K.meta.json"
        meta.write_text('{"filing_date": "2025-10-31"}')
        assert _doc_id("AAPL", meta) == "AAPL_10K_2025"

    def test_doc_id_without_meta(self, tmp_path):
        assert _doc_id("AAPL", tmp_path / "missing.json") == "AAPL_10K"

    def test_contradiction_feedback_lists_only_contradicted(self):
        c1 = Claim(claim_id="a", query_id="q", text="good", cited_chunk_ids=[])
        c2 = Claim(claim_id="b", query_id="q", text="bad", cited_chunk_ids=[])
        c1.verdict, c2.verdict = Verdict.SUPPORTED, Verdict.CONTRADICTED
        c2.judge_reason = "conflicts"
        fb = pipeline._contradiction_feedback([c1, c2])
        assert "bad" in fb and "good" not in fb


class TestCoverageDetection:
    def test_enumeration_asks_detected(self):
        for q in ["What are the biggest risk factors?",
                  "Who are Apple's main competitors?",
                  "List all business segments",
                  "特斯拉的主要风险有哪些？"]:
            assert pipeline.is_coverage_question(q), q

    def test_factual_and_numeric_asks_not_detected(self):
        for q in ["What was FY2025 revenue?",
                  "How much cash was spent on buybacks?",
                  "Did profitability improve?"]:
            assert not pipeline.is_coverage_question(q), q


class TestMMR:
    def test_diversifies_away_from_near_duplicates(self):
        from app.retrieval import _mmr
        # rows: (id, doc, ticker, section, text, embedding, distance)
        mk = lambda i, emb, d: (f"c{i}", "d", "T", "s", "t", emb, d)
        rows = [
            mk(0, [1.0, 0.0], 0.00),   # best match
            mk(1, [0.999, 0.04], 0.01),  # near-duplicate of c0
            mk(2, [0.0, 1.0], 0.30),   # different topic
        ]
        picked = [r[0] for r in _mmr(rows, k=2)]
        assert picked == ["c0", "c2"]  # duplicate skipped for the new topic
