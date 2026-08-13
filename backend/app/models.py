"""Domain data models for FinSignal Phase 1 (design-doc Section 7).

These are plain dataclasses / enums shared across the pipeline. Persistence
maps them onto the three tables in ``schema.sql``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    """Judge verdict for a single claim (design-doc Section 3, step 6)."""

    SUPPORTED = "SUPPORTED"
    NOT_ENOUGH_INFO = "NOT_ENOUGH_INFO"
    CONTRADICTED = "CONTRADICTED"


class ClaimStatus(str, Enum):
    """Display status derived from a verdict (requirements.md credibility rules).

    SUPPORTED        -> OK       (shown normally with citation link)
    NOT_ENOUGH_INFO  -> WARNING  (shown, marked "unverified")
    CONTRADICTED     -> ERROR    (blocked, not shown, triggers regeneration)
    """

    OK = "OK"
    WARNING = "WARNING"
    ERROR = "ERROR"


# Verdict -> display status mapping, per the credibility rules. Kept here as the
# single source of truth so generation/assembler/eval all agree.
VERDICT_TO_STATUS: dict[Verdict, ClaimStatus] = {
    Verdict.SUPPORTED: ClaimStatus.OK,
    Verdict.NOT_ENOUGH_INFO: ClaimStatus.WARNING,
    Verdict.CONTRADICTED: ClaimStatus.ERROR,
}


@dataclass
class Chunk:
    """A narrative chunk of a filing plus its embedding (chunks table)."""

    chunk_id: str
    doc_id: str
    ticker: str
    text: str
    section: str | None = None
    embedding: list[float] | None = None


@dataclass
class Claim:
    """A generated claim with citations and (after judging) its verdict."""

    claim_id: str
    query_id: str
    text: str
    cited_chunk_ids: list[str] = field(default_factory=list)
    verdict: Verdict | None = None
    judge_reason: str | None = None


@dataclass
class TestCase:
    """One golden-set evaluation case (test_cases table)."""

    case_id: str
    ticker: str
    question: str
    expected_answer_snippet: str | None = None
    expected_chunk_id: str | None = None
