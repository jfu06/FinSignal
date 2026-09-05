"""FinanceBench dataset access + document→EDGAR resolution.

FinanceBench (Islam et al., 2023, arXiv:2311.11944) open-source split:
150 expert-annotated questions over 84 real filings. We run its 10-K subset
(64 docs / 112 questions / 31 companies) as an EXTERNAL benchmark — a
capability score reported beside the release gate, never merged into it.

The dataset ships no filing text, only metadata. Rather than scraping the
(sometimes dead) cloudfront doc_links, each document is re-fetched from SEC
EDGAR — the authoritative source — located by (company CIK, fiscal year):

  CIK:  extracted from any of the company's doc_links when present
        (cloudfront URLs embed ``CIK-##########``), else matched by company
        name against SEC's official registry, else a manual override
        (Activision Blizzard was delisted after the Microsoft acquisition;
        AMD's legal name "Advanced Micro Devices" doesn't prefix-match).
  Year: ``doc_period`` == calendar year of the fiscal period end, which is
        exactly EDGAR's reportDate year (also our FY-label convention).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.edgar import load_ticker_map

BENCH_DIR = Path(__file__).resolve().parent.parent / "data" / "benchmark"
QUESTIONS_FILE = BENCH_DIR / "financebench_open_source.jsonl"
DOCS_FILE = BENCH_DIR / "financebench_document_information.jsonl"
RAW_DIR = BENCH_DIR / "raw"

_CIK_IN_LINK = re.compile(r"CIK-(\d{10})")

# Companies whose CIK can't be derived from doc_links or a name-prefix match.
_MANUAL_CIK = {
    "Activision Blizzard": "0000718877",  # delisted 2023 (Microsoft acquisition)
    "AMD": "0000002488",                  # legal name: Advanced Micro Devices
    "Foot Locker": "0000850209",          # delisted 2025 (Dick's acquisition)
    "Coca-Cola": "0000021344",            # prefix-ambiguous (FEMSA, Consolidated…)
}

# Filings whose financial statements are incorporated by reference from an
# exhibit — the primary document is only the 10-K wrapper, so these extra
# files must be appended to get the statements the questions ask about.
EXTRA_DOC_FILES: dict[str, list[str]] = {
    "CVSHEALTH_2018_10K": ["ex131.htm"],  # Exhibit 13.1: annual report w/ financials
}


@dataclass(frozen=True)
class BenchDoc:
    doc_name: str        # FinanceBench id, doubles as our chunks.doc_id
    company: str
    doc_type: str        # 10k / 10q / 8k / Earnings
    fy: int              # doc_period == fiscal year of the period end
    cik: str             # zero-padded 10 digits


@dataclass(frozen=True)
class BenchCase:
    financebench_id: str
    doc_name: str
    company: str
    question_type: str   # metrics-generated / domain-relevant / novel-generated
    question: str
    answer: str
    justification: str
    evidence_texts: list[str]


def _read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower().replace("-", " ")).strip()


def _resolve_ciks(doc_rows: list[dict], companies: set[str]) -> dict[str, str]:
    """Company → CIK via doc_link extraction, name match, or manual map."""

    out: dict[str, str] = {}
    for row in doc_rows:  # any doc_link with an embedded CIK settles the company
        m = _CIK_IN_LINK.search(row.get("doc_link") or "")
        if m and row["company"] in companies:
            out.setdefault(row["company"], m.group(1))

    unresolved = companies - out.keys()
    if unresolved:
        by_title = {
            _normalize(entry["title"]): entry["cik"]
            for entry in load_ticker_map().values()
        }
        for company in sorted(unresolved):
            if company in _MANUAL_CIK:
                out[company] = _MANUAL_CIK[company]
                continue
            want = _normalize(company)
            hits = {cik for title, cik in by_title.items()
                    if title == want or title.startswith(want + " ")}
            if len(hits) == 1:
                out[company] = hits.pop()
    missing = sorted(companies - out.keys())
    if missing:
        raise ValueError(f"Could not resolve CIK for: {missing} — "
                         f"add them to _MANUAL_CIK in {__name__}")
    return out


def load_benchmark(doc_types: tuple[str, ...] = ("10k",),
                   ) -> tuple[list[BenchDoc], list[BenchCase]]:
    """Return (documents, cases) for the requested doc types, CIKs resolved."""

    q_rows = _read_jsonl(QUESTIONS_FILE)
    doc_rows = _read_jsonl(DOCS_FILE)
    doc_meta = {r["doc_name"]: r for r in doc_rows}

    wanted_docs = sorted({
        q["doc_name"] for q in q_rows
        if doc_meta[q["doc_name"]]["doc_type"] in doc_types
    })
    companies = {doc_meta[d]["company"] for d in wanted_docs}
    ciks = _resolve_ciks(doc_rows, companies)

    docs = [
        BenchDoc(
            doc_name=d,
            company=doc_meta[d]["company"],
            doc_type=doc_meta[d]["doc_type"],
            fy=int(doc_meta[d]["doc_period"]),
            cik=ciks[doc_meta[d]["company"]],
        )
        for d in wanted_docs
    ]
    doc_names = {d.doc_name for d in docs}
    cases = [
        BenchCase(
            financebench_id=q["financebench_id"],
            doc_name=q["doc_name"],
            company=q["company"],
            question_type=q["question_type"],
            question=q["question"],
            answer=str(q["answer"]),
            justification=str(q.get("justification") or ""),
            evidence_texts=[e.get("evidence_text", "")
                            for e in (q.get("evidence") or [])],
        )
        for q in q_rows
        if q["doc_name"] in doc_names
    ]
    return docs, cases
