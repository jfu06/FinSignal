"""Materiality signals for risk claims — extracted, never judged.

10-Ks deliberately do not rank risks by severity, so FinSignal never
asserts "the biggest risk is X". But the document carries OBSERVABLE
importance signals analysts read for themselves, and those we can extract
deterministically (zero LLM):

- quantified: the claim itself carries figures that match its cited
  evidence (a €500M fine reads heavier than three lines of boilerplate);
  a numberless claim citing a chunk that happens to contain numbers is
  NOT quantified
- realized: the filing uses have-happened language ("have adversely
  affected") rather than hypothetical "could" — the strongest signal
- echoes: the same topic resurfaces in OTHER sections of the filing
  (MD&A, Legal Proceedings) — lawyers repeat what matters

Every signal is a checkable document fact; ranking stays with the analyst.
"""

from __future__ import annotations

import re

from .config import Settings, get_settings
from .models import Claim
from .span_overlap import number_match

# Realized-risk language: past/perfect constructions the filing uses when a
# risk has already bitten. Ordered; first match's phrase is surfaced.
_REALIZED_PATTERNS = [
    r"ha(?:s|ve)(?: been)?(?: materially)? adversely affect(?:ed)?",
    r"ha(?:s|ve) (?:materially )?adversely impact(?:ed)?",
    r"ha(?:s|ve) experienced",
    r"ha(?:s|ve) resulted in",
    r"ha(?:s|ve) (?:in the past )?(?:suffered|incurred)",
    r"has and could",
    r"were adversely affected",
]
_REALIZED_RE = re.compile("|".join(f"({p})" for p in _REALIZED_PATTERNS),
                          re.IGNORECASE)




def _norm_section(section: str | None) -> str:
    return (section or "").strip().lower()


def claim_signals(
    claim: Claim,
    evidence_texts: list[str],
    cited_sections: list[str],
    ticker: str,
    settings: Settings | None = None,
    doc_id: str | None = None,
) -> dict:
    """Signals for one risk claim. Deterministic; failures degrade to {}."""

    settings = settings or get_settings()
    evidence = " ".join(evidence_texts)

    signals: dict = {}
    # quantified: the claim states figures AND they all check out against
    # the cited text — a claim-level fact, not "the chunk contains numbers"
    if number_match(claim.text, evidence) == 1.0:
        signals["quantified"] = True
    m = _REALIZED_RE.search(evidence)
    if m:
        signals["realized"] = m.group(0)

    # Cross-section echoes: retrieve chunks similar to the claim and count
    # sections OTHER than where it was cited. Local embedding + one
    # pgvector query — no LLM, ~150ms.
    try:
        from .retrieval import retrieve

        cited = {_norm_section(s) for s in cited_sections}
        hits = retrieve(claim.text, ticker, k=5, settings=settings,
                        doc_id=doc_id)
        echoes = sorted({
            (c.section or "").strip() for c in hits
            if c.section and _norm_section(c.section) not in cited
        })
        if echoes:
            signals["echoes"] = echoes[:3]
    except Exception:  # noqa: BLE001 — signals must never break an answer
        pass
    return signals


def annotate_risk_claims(
    report: dict,
    claims: list[Claim],
    chunks_by_id: dict,
    ticker: str,
    settings: Settings | None = None,
    doc_id: str | None = None,
) -> None:
    """Attach a ``signals`` dict to each risk claim in the report, in place."""

    by_id = {c.claim_id: c for c in claims}
    for claim_dict in report.get("claims", []):
        if claim_dict.get("kind") != "risk":
            continue
        claim = by_id.get(claim_dict.get("claim_id"))
        if claim is None:
            continue
        ev = [chunks_by_id[cid].text for cid in claim.cited_chunk_ids
              if cid in chunks_by_id]
        sections = [chunks_by_id[cid].section or ""
                    for cid in claim.cited_chunk_ids if cid in chunks_by_id]
        if not ev:
            continue
        signals = claim_signals(claim, ev, sections, ticker,
                                settings=settings, doc_id=doc_id)
        if signals:
            claim_dict["signals"] = signals
