"""Span-overlap checks: a deterministic second signal beside the LLM judge.

The judge is authoritative for the WARNING/ERROR credibility mapping; these
checks are cheap, reproducible cross-checks surfaced in the report and
aggregated by the eval harness. Two signals with different language behavior
(claims are often Chinese while the 10-K evidence is English):

1. ``number_match`` — language-independent. Every numeric figure in the claim
   (3+ digits) should appear in the cited evidence. Comparison is VALUE-based
   with unit normalization: scale words are applied (billion/million/亿/万),
   a x1000 scale ladder bridges unit conventions (evidence tables print
   "416,161" in millions where a claim says "$416.2 billion"), and a 0.2%
   relative tolerance absorbs honest display rounding. Pure digit-string
   matching flagged correctly-converted figures as "not found" — a false
   alarm that undermined every real flag.
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


_SCALE_WORDS = {
    "trillion": 1e12, "billion": 1e9, "bn": 1e9, "million": 1e6, "mn": 1e6,
    "thousand": 1e3,
    "万亿": 1e12, "十亿": 1e9, "亿": 1e8, "百万": 1e6, "万": 1e4,
}
_VALUE_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*"
    r"(trillion|billion|bn|million|mn|thousand|万亿|十亿|亿|百万|万)?",
    re.IGNORECASE,
)
# Bridges unit conventions between claim and evidence (a table printed in
# millions vs a claim written in billions differ by x1000 steps).
_SCALE_LADDER = (1e-9, 1e-6, 1e-3, 1.0, 1e3, 1e6, 1e9)
_REL_TOLERANCE = 0.002  # 0.2%: display rounding ($416.2B vs 416,161M), no more


def _values(text: str) -> list[float]:
    """Numeric values in the text, scale words applied, small numbers skipped."""

    out: list[float] = []
    for num, scale in _VALUE_RE.findall(text):
        digits = re.sub(r"\D", "", num)
        if len(digits) < MIN_DIGITS:
            continue  # "52 weeks", "10%" — parity with the old behavior
        value = float(num.replace(",", ""))
        if (not scale and "," not in num and "." not in num
                and 1900 <= value <= 2100):
            continue  # a year ("fiscal 2025"), not a financial figure
        if scale:
            value *= _SCALE_WORDS[scale.lower()]
        out.append(value)
    return out


def _value_found(claim_value: float, evidence_values: list[float]) -> bool:
    for ev in evidence_values:
        if ev == 0:
            continue
        for k in _SCALE_LADDER:
            if abs(claim_value * k - ev) / abs(ev) < _REL_TOLERANCE:
                return True
    return False


def number_match(claim_text: str, evidence_text: str) -> float | None:
    """Fraction of the claim's numbers found in the evidence (None if no numbers).

    Value-based with unit normalization — "$416.2 billion" matches an
    evidence table's "416,161" (millions); a fabricated figure still fails.
    """

    claim_values = _values(claim_text)
    if not claim_values:
        return None
    evidence_values = _values(evidence_text)
    matched = sum(1 for v in claim_values
                  if _value_found(v, evidence_values))
    return matched / len(claim_values)


def values_match_facts(claim_text: str, fact_values: list[float]) -> bool:
    """True if EVERY figure in the claim matches some official fact value.

    The XBRL rescue channel: a claim can quote a company-level total its
    cited excerpt doesn't repeat (the text explains, the table sits in
    another chunk). Before flagging, check the figures against the
    company's official XBRL facts — same scale ladder and tolerance.
    """

    claim_values = _values(claim_text)
    if not claim_values:
        return False
    return all(_value_found(v, fact_values) for v in claim_values)


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
