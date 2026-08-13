"""Span-overlap checks: a deterministic second signal beside the LLM judge.

The judge is authoritative for the WARNING/ERROR credibility mapping; these
checks are cheap, reproducible cross-checks surfaced in the report and
aggregated by the eval harness. Two signals with different language behavior
(claims are often Chinese while the 10-K evidence is English):

1. ``number_match`` — language-independent. Every numeric figure in the claim
   (3+ digits) should appear in the cited evidence. Numbers are compared as
   bare digit sequences, which makes cross-format matches work:
   ``4,161.61亿`` -> ``416161`` matches evidence ``$416,161 (million)``.
   A claim quoting figures its own citations don't contain is a red flag the
   judge might have missed.
2. ``token_overlap`` — English content-word overlap between claim and
   evidence (ROUGE-1-precision-like). Only meaningful when the claim actually
   contains enough English content words; otherwise reported as ``None``
   (N/A) rather than a misleading zero for Chinese claims.

Pure functions, no I/O, no LLM — fully unit-tested.
"""

from __future__ import annotations

import re

# Digit runs possibly broken by thousand separators / decimal points,
# e.g. "416,161", "4,161.61", "2025".
_NUMBER_RE = re.compile(r"\d[\d,. ]*\d|\d")
# English words with some content (4+ letters filters the/of/is debris).
_EN_WORD_RE = re.compile(r"[A-Za-z]{4,}")

_STOPWORDS = {
    "that", "this", "these", "those", "with", "from", "have", "been", "were",
    "will", "would", "could", "should", "their", "there", "which", "while",
    "during", "compared", "company", "companys", "fiscal", "year", "years",
    "including", "such", "other", "more", "than", "also", "into", "over",
    "under", "about", "between", "primarily", "certain", "may", "and", "the",
}

MIN_DIGITS = 3          # ignore 1-2 digit numbers ("52 weeks", list ordinals)
MIN_EN_TOKENS = 3       # below this, token overlap is N/A (e.g. Chinese claim)


def _digit_sequences(text: str) -> set[str]:
    """Extract numbers as bare digit strings (>= MIN_DIGITS digits)."""

    out: set[str] = set()
    for match in _NUMBER_RE.findall(text):
        digits = re.sub(r"\D", "", match)
        if len(digits) >= MIN_DIGITS:
            out.add(digits)
    return out


def _en_tokens(text: str) -> set[str]:
    return {
        w.lower() for w in _EN_WORD_RE.findall(text)
        if w.lower() not in _STOPWORDS
    }


def number_match(claim_text: str, evidence_text: str) -> float | None:
    """Fraction of the claim's numbers found in the evidence (None if no numbers).

    Evidence digits are matched by substring containment so a claim-side
    rounding of a longer figure ("416,161" quoted as "416,000") does NOT
    count, but identical digit sequences embedded in wider evidence text do.
    """

    claim_numbers = _digit_sequences(claim_text)
    if not claim_numbers:
        return None
    evidence_numbers = _digit_sequences(evidence_text)
    matched = sum(1 for n in claim_numbers if n in evidence_numbers)
    return matched / len(claim_numbers)


def token_overlap(claim_text: str, evidence_text: str) -> float | None:
    """Fraction of the claim's English content words present in the evidence.

    Returns None (N/A) when the claim has too few English content words for
    the metric to mean anything — typical for Chinese-language claims.
    """

    claim_tokens = _en_tokens(claim_text)
    if len(claim_tokens) < MIN_EN_TOKENS:
        return None
    evidence_tokens = _en_tokens(evidence_text)
    return len(claim_tokens & evidence_tokens) / len(claim_tokens)


def check_claim(claim_text: str, evidence_texts: list[str]) -> dict:
    """Run both checks for one claim against ALL its cited evidence combined.

    Returns::

        {"number_match": float|None,   # None = claim contains no numbers
         "token_overlap": float|None,  # None = N/A (non-English claim)
         "flagged": bool}              # True = numbers cited evidence lacks

    ``flagged`` is deliberately based on the language-independent signal only:
    a claim whose numbers aren't all found in its citations deserves a look
    regardless of what the judge said.
    """

    evidence = "\n".join(evidence_texts)
    numbers = number_match(claim_text, evidence)
    tokens = token_overlap(claim_text, evidence)
    return {
        "number_match": numbers,
        "token_overlap": tokens,
        "flagged": numbers is not None and numbers < 1.0,
    }
