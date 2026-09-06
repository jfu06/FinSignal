"""Response assembly + credibility mapping (design-doc §3 step 7).

Pure functions (no I/O, no LLM) — fully unit-testable. Applies the
credibility rules from requirements.md:

  SUPPORTED       -> OK      : shown normally with citation links
  NOT_ENOUGH_INFO -> WARNING : shown, marked "unverified", listed under
                               unsupported claims (user must acknowledge)
  CONTRADICTED    -> ERROR   : blocked — NOT displayed; recorded for the
                               regeneration path

unsupported_rate = (NOT_ENOUGH_INFO + CONTRADICTED) / total claims (§4).
"""

from __future__ import annotations

from typing import Any

from .models import Chunk, Claim, ClaimStatus, VERDICT_TO_STATUS, Verdict
from .span_overlap import check_claim

DISCLAIMER = (
    "This analysis is generated automatically from cited public-filing "
    "excerpts, for reference only, and is not investment advice."
)

# Per-claim credibility score derived from the judge verdict. A simple,
# documented heuristic — the differentiator is the per-claim granularity, not
# the absolute number.
_CREDIBILITY: dict[Verdict, float] = {
    Verdict.SUPPORTED: 0.9,
    Verdict.NOT_ENOUGH_INFO: 0.4,
    Verdict.CONTRADICTED: 0.0,
}

# Full chunk text: a truncated preview once hid the supporting sentence
# (pandemic language sat at char ~350 of a cited chunk), making a correct
# citation look fabricated. Chunks are ~1KB; ship the whole thing.


def unsupported_rate(claims: list[Claim]) -> float:
    """(NOT_ENOUGH_INFO + CONTRADICTED) / total, 0.0 for an empty list."""

    if not claims:
        return 0.0
    bad = sum(
        1
        for c in claims
        if c.verdict in (Verdict.NOT_ENOUGH_INFO, Verdict.CONTRADICTED)
    )
    return bad / len(claims)


def _citation(chunk: Chunk) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "section": chunk.section,
        "preview": chunk.text,
    }


def assemble_report(
    query_id: str,
    question: str,
    ticker: str,
    summary: str,
    claims: list[Claim],
    chunks: list[Chunk],
) -> dict[str, Any]:
    """Build the final analysis report from judged claims.

    ERROR (CONTRADICTED) claims are excluded from the displayed list — only
    surfaced as blocked metadata. WARNING claims are displayed with an
    explicit ``unverified`` marker and repeated in ``unsupported_claims``.
    """

    chunks_by_id = {c.chunk_id: c for c in chunks}

    displayed: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    risk_flags: list[str] = []

    for claim in claims:
        verdict = claim.verdict or Verdict.NOT_ENOUGH_INFO  # fail-safe
        status = VERDICT_TO_STATUS[verdict]

        if status is ClaimStatus.ERROR:
            # Blocked: never displayed; recorded for logging/review.
            blocked.append(
                {
                    "claim_id": claim.claim_id,
                    "verdict": verdict.value,
                    "reason": claim.judge_reason,
                }
            )
            continue

        cited_chunks = [
            chunks_by_id[cid] for cid in claim.cited_chunk_ids if cid in chunks_by_id
        ]
        entry = {
            "claim_id": claim.claim_id,
            "text": claim.text,
            "kind": getattr(claim, "kind", "insight"),
            "status": status.value,
            "verdict": verdict.value,
            "credibility": _CREDIBILITY[verdict],
            "unverified": status is ClaimStatus.WARNING,
            "judge_reason": claim.judge_reason,
            # deterministic second signal beside the judge (span_overlap.py)
            "span_overlap": check_claim(claim.text, [c.text for c in cited_chunks]),
            "citations": [_citation(c) for c in cited_chunks],
        }
        displayed.append(entry)

        if status is ClaimStatus.WARNING:
            unsupported.append(
                {
                    "claim_id": claim.claim_id,
                    "text": claim.text,
                    "reason": claim.judge_reason,
                }
            )
        if entry["kind"] == "risk" and status is ClaimStatus.OK:
            # WARNING claims stay behind the acknowledgment gate — the flags
            # summary must never present unverified text as plain risk facts
            risk_flags.append(claim.text)

    return {
        "query_id": query_id,
        "ticker": ticker,
        "question": question,
        "summary": summary,
        "claims": displayed,
        "risk_flags": risk_flags,
        "unsupported_claims": unsupported,
        "blocked_claims": blocked,
        "unsupported_rate": unsupported_rate(claims),
        "disclaimer": DISCLAIMER,
    }
