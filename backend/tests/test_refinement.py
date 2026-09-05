"""Unit tests for the bounded retrieval-refinement loop (mocked LLM + retrieval).

The contract under test: model picks the branch, code owns the control flow —
round/budget caps, k growth, fail-safe defaults, and union top-up.
"""

from __future__ import annotations

import pytest

import app.refinement as refinement
from app.models import Chunk
from app.refinement import MAX_K, MAX_ROUNDS, retrieve_refined
from tests.llm_fakes import FakeAnthropic, make_settings, tool_response


def chunk(cid: str) -> Chunk:
    return Chunk(chunk_id=cid, doc_id="D", ticker="AAPL",
                 text=f"text of {cid}", section="Item 1")


@pytest.fixture
def rec_retrieve(monkeypatch):
    """Scriptable fake for refinement.retrieve; records every call."""

    calls: list[dict] = []
    script: list[list[Chunk]] = []

    def fake(query, ticker, k, settings, query_id, doc_id=None):  # noqa: ANN001
        calls.append({"query": query, "k": k, "doc_id": doc_id})
        return script.pop(0) if script else [chunk("c1")]

    monkeypatch.setattr(refinement, "retrieve", fake)
    return calls, script


def assess(decision: str, new_query: str | None = None):
    data = {"decision": decision, "reason": "test"}
    if new_query is not None:
        data["new_query"] = new_query
    return tool_response(data)


class TestDecisions:
    def test_enough_on_first_round_stops_immediately(self, rec_retrieve, tmp_path):
        calls, script = rec_retrieve
        script.append([chunk("c1"), chunk("c2")])
        FakeAnthropic.queue = [assess("ENOUGH")]

        chunks, trace = retrieve_refined("q?", "AAPL", settings=make_settings(tmp_path))

        assert len(calls) == 1                       # no second retrieval
        assert len(FakeAnthropic.calls) == 1         # exactly one assessment
        assert trace.rounds == 1 and trace.decisions == ["ENOUGH"]
        assert [c.chunk_id for c in chunks] == ["c1", "c2"]

    def test_rewrite_changes_query(self, rec_retrieve, tmp_path):
        calls, script = rec_retrieve
        script += [[chunk("c1")], [chunk("c9")]]
        FakeAnthropic.queue = [assess("REWRITE", "distribution channels resellers"),
                               assess("ENOUGH")]

        chunks, trace = retrieve_refined("中文问题", "AAPL",
                                         settings=make_settings(tmp_path))

        assert calls[0]["query"] == "中文问题"
        assert calls[1]["query"] == "distribution channels resellers"
        assert trace.final_query == "distribution channels resellers"
        assert "c9" in {c.chunk_id for c in chunks}

    def test_expand_doubles_k(self, rec_retrieve, tmp_path):
        calls, script = rec_retrieve
        script += [[chunk("c1")], [chunk("c1"), chunk("c2")]]
        FakeAnthropic.queue = [assess("EXPAND"), assess("ENOUGH")]

        _, trace = retrieve_refined("q?", "AAPL", settings=make_settings(tmp_path))

        assert calls[0]["k"] == 6 and calls[1]["k"] == 12
        assert trace.final_k == 12

    def test_give_up_stops_without_more_retrieval(self, rec_retrieve, tmp_path):
        calls, script = rec_retrieve
        script.append([chunk("c1")])
        FakeAnthropic.queue = [assess("GIVE_UP")]

        _, trace = retrieve_refined("q?", "AAPL", settings=make_settings(tmp_path))

        assert len(calls) == 1
        assert trace.decisions == ["GIVE_UP"]


class TestGuardrails:
    def test_round_budget_is_hard_capped(self, rec_retrieve, tmp_path):
        calls, script = rec_retrieve
        script += [[chunk(f"r{i}")] for i in range(MAX_ROUNDS)]
        # assessor always wants more — code must stop anyway
        FakeAnthropic.queue = [assess("EXPAND")] * (MAX_ROUNDS - 1)

        _, trace = retrieve_refined("q?", "AAPL", settings=make_settings(tmp_path))

        assert len(calls) == MAX_ROUNDS
        assert len(FakeAnthropic.calls) == MAX_ROUNDS - 1  # no wasted final assess
        assert trace.rounds == MAX_ROUNDS

    def test_k_never_exceeds_cap(self, rec_retrieve, tmp_path):
        calls, script = rec_retrieve
        script += [[chunk("a")], [chunk("b")], [chunk("c")]]
        FakeAnthropic.queue = [assess("EXPAND"), assess("EXPAND")]

        _, trace = retrieve_refined("q?", "AAPL", settings=make_settings(tmp_path))

        assert max(c["k"] for c in calls) <= MAX_K

    def test_invalid_decision_fails_safe_to_enough(self, rec_retrieve, tmp_path):
        calls, script = rec_retrieve
        script.append([chunk("c1")])
        FakeAnthropic.queue = [assess("DO_SOMETHING_WEIRD")]

        _, trace = retrieve_refined("q?", "AAPL", settings=make_settings(tmp_path))

        assert len(calls) == 1                       # behaved like Phase-1
        assert trace.decisions == ["ENOUGH"]

    def test_rewrite_without_query_degrades_to_expand(self, rec_retrieve, tmp_path):
        calls, script = rec_retrieve
        script += [[chunk("c1")], [chunk("c2")]]
        FakeAnthropic.queue = [assess("REWRITE"), assess("ENOUGH")]  # no new_query

        retrieve_refined("q?", "AAPL", settings=make_settings(tmp_path))

        assert calls[1]["query"] == "q?"             # query unchanged
        assert calls[1]["k"] == 12                   # but coverage still grew

    def test_union_topup_preserves_round1_chunks_after_drifted_rewrite(
            self, rec_retrieve, tmp_path):
        calls, script = rec_retrieve
        script += [[chunk("good1"), chunk("good2")], [chunk("drift1")]]
        FakeAnthropic.queue = [assess("REWRITE", "off in the weeds"),
                               assess("ENOUGH")]

        chunks, _ = retrieve_refined("q?", "AAPL", settings=make_settings(tmp_path))

        ids = [c.chunk_id for c in chunks]
        assert ids[0] == "drift1"                    # last round first
        assert {"good1", "good2"} <= set(ids)        # round 1 not lost
        assert len(ids) == len(set(ids))             # deduplicated

    def test_decisions_logged_as_events(self, rec_retrieve, tmp_path):
        import json

        _, script = rec_retrieve
        script += [[chunk("c1")], [chunk("c2")]]
        FakeAnthropic.queue = [assess("EXPAND"), assess("ENOUGH")]
        settings = make_settings(tmp_path)

        retrieve_refined("q?", "AAPL", settings=settings, query_id="q1")

        events = [json.loads(l) for l in
                  settings.log_path.read_text().strip().splitlines()]
        refined = [e for e in events if e["event"] == "retrieval_refined"]
        assert [e["decision"] for e in refined] == ["EXPAND", "ENOUGH"]
        assert all(e["query_id"] == "q1" for e in refined)
