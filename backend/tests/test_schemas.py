"""Unit tests for the Pydantic tool-output schemas (pure, no I/O)."""

from __future__ import annotations

import json

from app.schemas import (
    AnswerPayload,
    AssessmentPayload,
    QuestionsPayload,
    VerdictsPayload,
)

GOOD_CLAIM = {"text": "t", "cited_chunk_ids": ["c1"], "kind": "insight"}


class TestAnswerPayload:
    def test_well_formed(self):
        p = AnswerPayload.from_tool_input({"summary": "s", "claims": [GOOD_CLAIM]})
        assert p is not None
        assert p.summary == "s"
        assert p.claims[0].cited_chunk_ids == ["c1"]

    def test_envelope_unwrapped(self):
        p = AnswerPayload.from_tool_input(
            {"parameter": {"summary": "s", "claims": [GOOD_CLAIM]}})
        assert p is not None and len(p.claims) == 1

    def test_envelope_with_claims_array_under_arbitrary_key(self):
        # Observed in production: {"parameter name": [ ...claims... ]}
        p = AnswerPayload.from_tool_input({"parameter name": [GOOD_CLAIM]})
        assert p is not None
        assert len(p.claims) == 1 and p.claims[0].cited_chunk_ids == ["c1"]

    def test_envelope_with_json_string_array(self):
        p = AnswerPayload.from_tool_input({"input": json.dumps([GOOD_CLAIM])})
        assert p is not None and len(p.claims) == 1

    def test_envelope_with_json_string(self):
        raw = {"input": json.dumps({"summary": "s", "claims": [GOOD_CLAIM]})}
        p = AnswerPayload.from_tool_input(raw)
        assert p is not None and len(p.claims) == 1

    def test_claims_as_json_string(self):
        p = AnswerPayload.from_tool_input(
            {"summary": "s", "claims": json.dumps([GOOD_CLAIM])})
        assert p is not None and len(p.claims) == 1

    def test_flattened_single_claim(self):
        p = AnswerPayload.from_tool_input(
            {"claims": "the claim text", "cited_chunk_ids": ["c1"], "kind": "risk"})
        assert p is not None
        assert p.claims[0].text == "the claim text"
        assert p.claims[0].kind == "risk"

    def test_claims_as_single_dict(self):
        p = AnswerPayload.from_tool_input({"summary": "s", "claims": GOOD_CLAIM})
        assert p is not None and len(p.claims) == 1

    def test_bare_string_items_kept_without_cites(self):
        p = AnswerPayload.from_tool_input({"summary": "s", "claims": ["bare"]})
        assert p is not None
        assert p.claims[0].cited_chunk_ids == []

    def test_empty_text_items_dropped(self):
        p = AnswerPayload.from_tool_input(
            {"summary": "s", "claims": [GOOD_CLAIM, {"text": "  "}]})
        assert p is not None and len(p.claims) == 1

    def test_invalid_kind_defaults_to_insight(self):
        p = AnswerPayload.from_tool_input(
            {"summary": "s", "claims": [{**GOOD_CLAIM, "kind": "prophecy"}]})
        assert p.claims[0].kind == "insight"

    def test_string_cite_coerced_to_list(self):
        p = AnswerPayload.from_tool_input(
            {"summary": "s", "claims": [{"text": "t", "cited_chunk_ids": "c1"}]})
        assert p.claims[0].cited_chunk_ids == ["c1"]

    def test_unsalvageable_returns_none(self):
        assert AnswerPayload.from_tool_input(None) is None
        assert AnswerPayload.from_tool_input({"summary": "s"}) is None
        assert AnswerPayload.from_tool_input({"claims": 42}) is None
        assert AnswerPayload.from_tool_input({"weird": "not json {"}) is None


class TestVerdictsPayload:
    def test_junk_entries_dropped(self):
        p = VerdictsPayload.from_tool_input({"verdicts": [
            {"claim_id": "a", "verdict": "SUPPORTED", "reason": "ok"},
            "not a dict",
            {"verdict": "SUPPORTED"},          # no claim_id
        ]})
        assert [v.claim_id for v in p.verdicts] == ["a"]

    def test_non_dict_input_yields_empty(self):
        assert VerdictsPayload.from_tool_input(None).verdicts == []


class TestAssessmentPayload:
    def test_none_input_yields_defaults(self):
        p = AssessmentPayload.from_tool_input(None)
        assert p.decision == "" and p.new_query == ""

    def test_null_fields_coerced(self):
        p = AssessmentPayload.from_tool_input(
            {"decision": "REWRITE", "new_query": None, "reason": None})
        assert p.new_query == ""


class TestQuestionsPayload:
    def test_junk_items_dropped(self):
        p = QuestionsPayload.from_tool_input({"items": [
            {"chunk_id": "c1", "question": "Q?"},
            {"chunk_id": "", "question": "Q?"},
            {"chunk_id": "c2"},
            "junk",
        ]})
        assert [i.chunk_id for i in p.items] == ["c1"]
