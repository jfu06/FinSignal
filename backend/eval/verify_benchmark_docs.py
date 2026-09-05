"""Verify each fetched benchmark filing is the document FinanceBench meant.

Fiscal-year labels are ambiguous for non-December fiscal-year ends: Walmart
labels the FY ending 2018-01-31 "FY2018" (end-year convention) while J&J
labels the FY ending 2022-01-02 "FY2021" (start-year convention) — so a
year-based EDGAR lookup can fetch a filing one year off. The arbiter is the
dataset itself: FinanceBench evidence_text is quoted verbatim from the
intended document, so the right filing must contain it.

For every doc, slide an 8-word shingle window over each question's evidence
and test containment in the whitespace-normalized fetched text. A doc where
NO question's evidence matches is flagged as suspect (likely wrong year, or
heavy table mangling — triage the flagged list by report_date).

Usage:  python -m eval.verify_benchmark_docs
"""

from __future__ import annotations

import json
import re
from collections import defaultdict

from .benchmark_data import RAW_DIR, load_benchmark

_WORD = re.compile(r"[A-Za-z0-9.,$%()\-]+")
SHINGLE = 8


def _norm_words(text: str) -> list[str]:
    return _WORD.findall(text)


def evidence_hit(evidence: str, doc_norm: str) -> bool:
    """True if any 8-word run of the evidence appears verbatim in the doc."""

    words = _norm_words(evidence)
    if len(words) < SHINGLE:
        return bool(words) and " ".join(words) in doc_norm
    step = max(1, (len(words) - SHINGLE) // 40)  # ~40 probes per evidence
    for i in range(0, len(words) - SHINGLE + 1, step):
        if " ".join(words[i:i + SHINGLE]) in doc_norm:
            return True
    return False


def main() -> int:
    docs, cases = load_benchmark()
    by_doc: dict[str, list] = defaultdict(list)
    for c in cases:
        by_doc[c.doc_name].append(c)

    suspects: list[str] = []
    for doc in docs:
        path = RAW_DIR / f"{doc.doc_name}.txt"
        if not path.exists():
            print(f"{doc.doc_name}: NOT FETCHED")
            suspects.append(doc.doc_name)
            continue
        doc_norm = " ".join(_norm_words(path.read_text(encoding="utf-8")))
        doc_cases = by_doc[doc.doc_name]
        hits = sum(
            any(evidence_hit(ev, doc_norm) for ev in c.evidence_texts if ev)
            for c in doc_cases
        )
        meta = json.loads(
            (RAW_DIR / f"{doc.doc_name}.meta.json").read_text(encoding="utf-8"))
        status = "ok" if hits else "SUSPECT"
        print(f"{doc.doc_name}: {hits}/{len(doc_cases)} questions' evidence "
              f"found (report_date={meta['report_date']}) {status}")
        if not hits:
            suspects.append(doc.doc_name)

    print(f"\n{len(suspects)} suspect docs: {suspects}")
    return 1 if suspects else 0


if __name__ == "__main__":  # pragma: no cover - operational entry point
    raise SystemExit(main())
