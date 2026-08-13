"""Pydantic schemas for every LLM structured (tool-call) output.

Each forced tool call in the pipeline has a schema here, parsed with
``from_tool_input(raw)`` — lenient where production traces showed the model
misbehaving, strict enough that unsalvageable shapes return ``None``/empty
and trigger the caller's retry/fail-safe path.

Salvage rules live in ``model_validator(mode="before")`` hooks so they are
declared next to the fields they protect, replacing the previous hand-rolled
normalization code. Observed malformed shapes covered (from jsonl traces):

- whole payload wrapped in a single-key envelope: ``{"parameter": {...}}``
- ``claims`` emitted as a JSON-encoded string
- a single claim object flattened to the top level
- ``claims`` as one dict instead of a list
- bare-string list items (become citation-less claims — the judge flags them)
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


# ----------------------------- generation --------------------------------


class ClaimItem(BaseModel):
    """One generated claim as returned by the record_answer tool."""

    text: str
    cited_chunk_ids: list[str] = Field(default_factory=list)
    kind: Literal["insight", "risk"] = "insight"

    @field_validator("kind", mode="before")
    @classmethod
    def _default_kind(cls, v: object) -> object:
        return v if v in ("insight", "risk") else "insight"

    @field_validator("cited_chunk_ids", mode="before")
    @classmethod
    def _coerce_cites(cls, v: object) -> object:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v


class AnswerPayload(BaseModel):
    """record_answer output: plain-language summary + cited claims."""

    summary: str = ""
    claims: list[ClaimItem] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _salvage(cls, data: object) -> dict:
        if not isinstance(data, dict):
            raise ValueError("tool input is not an object")

        # Single-key envelope: {"parameter": {...}} / {"input": "...json..."}
        if "claims" not in data and len(data) == 1:
            inner = next(iter(data.values()))
            if isinstance(inner, str):
                inner = json.loads(inner)  # raises -> unsalvageable
            if not isinstance(inner, dict):
                raise ValueError("envelope does not contain an object")
            data = inner

        claims = data.get("claims")
        if isinstance(claims, str):
            try:
                claims = json.loads(claims)
            except (json.JSONDecodeError, TypeError):
                if "cited_chunk_ids" in data:  # flattened single claim
                    claims = [{
                        "text": claims,
                        "cited_chunk_ids": data.get("cited_chunk_ids", []),
                        "kind": data.get("kind", "insight"),
                    }]
                else:
                    raise ValueError("claims is an unparseable string") from None
        if isinstance(claims, dict):
            claims = [claims]
        if not isinstance(claims, list):
            raise ValueError("claims is not a list")

        normalized = []
        for item in claims:
            if isinstance(item, str) and item.strip():
                normalized.append({"text": item})
            elif isinstance(item, dict) and str(item.get("text", "")).strip():
                normalized.append(item)
        return {"summary": data.get("summary") or "", "claims": normalized}

    @classmethod
    def from_tool_input(cls, raw: object) -> "AnswerPayload | None":
        """Parse leniently; None means unsalvageable (caller retries/aborts)."""

        try:
            return cls.model_validate(raw)
        except (ValidationError, ValueError, json.JSONDecodeError, TypeError):
            return None


# ----------------------------- verification ------------------------------


class VerdictEntry(BaseModel):
    """One judge verdict. ``verdict`` stays a plain string here: mapping an
    invalid label to NOT_ENOUGH_INFO is apply_verdicts' fail-safe job."""

    claim_id: str
    verdict: str = ""
    reason: str = ""


class VerdictsPayload(BaseModel):
    """record_verdicts output."""

    verdicts: list[VerdictEntry] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _drop_junk_entries(cls, data: object) -> dict:
        if not isinstance(data, dict):
            return {"verdicts": []}
        entries = data.get("verdicts")
        if not isinstance(entries, list):
            return {"verdicts": []}
        return {"verdicts": [
            e for e in entries
            if isinstance(e, dict) and str(e.get("claim_id", "")).strip()
        ]}

    @classmethod
    def from_tool_input(cls, raw: object) -> "VerdictsPayload":
        try:
            return cls.model_validate(raw)
        except ValidationError:  # pragma: no cover - _drop_junk_entries guards
            return cls()


# ----------------------------- refinement --------------------------------


class AssessmentPayload(BaseModel):
    """record_assessment output. ``decision`` validity is enforced by the
    refinement loop (invalid -> ENOUGH fail-safe), not here."""

    decision: str = ""
    new_query: str = ""
    reason: str = ""

    @field_validator("decision", "new_query", "reason", mode="before")
    @classmethod
    def _coerce_str(cls, v: object) -> str:
        return str(v) if v is not None else ""

    @classmethod
    def from_tool_input(cls, raw: object) -> "AssessmentPayload":
        try:
            return cls.model_validate(raw if isinstance(raw, dict) else {})
        except ValidationError:  # pragma: no cover - defensive
            return cls()


# ----------------------------- smoke eval --------------------------------


class QuestionItem(BaseModel):
    chunk_id: str
    question: str


class QuestionsPayload(BaseModel):
    """record_questions output (smoke-eval synthetic QA)."""

    items: list[QuestionItem] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _drop_junk_items(cls, data: object) -> dict:
        if not isinstance(data, dict):
            return {"items": []}
        items = data.get("items")
        if not isinstance(items, list):
            return {"items": []}
        return {"items": [
            i for i in items
            if isinstance(i, dict)
            and str(i.get("chunk_id", "")).strip()
            and str(i.get("question", "")).strip()
        ]}

    @classmethod
    def from_tool_input(cls, raw: object) -> "QuestionsPayload":
        try:
            return cls.model_validate(raw)
        except ValidationError:  # pragma: no cover - _drop_junk_items guards
            return cls()
