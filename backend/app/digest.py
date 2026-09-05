"""One-click annual report digest (product roadmap item ②).

Solves the retail cold-start problem: instead of facing an empty chat box,
the user gets a fixed-structure overview of a company — every narrative
sentence still claim-checked with citations, every figure still deterministic
with filing provenance. The digest is an entry point: each section invites
follow-up questions in the chat.

Structure:
  key_figures   — metrics layer directly (no LLM, no router): revenue + YoY,
                  net income, gross margin, R&D, operating cash flow
  4 sections    — business / performance drivers / risks / management focus,
                  each a full pipeline run (retrieve → generate → judge)

The four sections run IN PARALLEL (LLM calls are I/O bound), so digest
wall-time ≈ the slowest single section rather than the sum. Each section
fails soft: one bad section shows an error card, the rest still render.

Usage:
    python -m app.digest AAPL
"""

from __future__ import annotations

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from .config import Settings, get_settings
from .logging_utils import log_event
from .pipeline import answer_question
from .router import execute_numeric

Progress = Callable[[str], None]


def _noop(_: str) -> None:  # pragma: no cover - trivial
    pass


# Deterministic key figures — hand-built queries, router not involved.
FIGURE_QUERIES: list[dict] = [
    {"op": "yoy", "metric": "revenue"},
    {"op": "value", "metric": "net_income"},
    {"op": "ratio", "metric": "gross_margin"},
    {"op": "value", "metric": "research_and_development"},
    {"op": "value", "metric": "operating_cash_flow"},
]

# (key, title, question) — questions are phrased to land in different parts
# of a 10-K (Item 1 / Item 7 / Item 1A / MD&A emphasis).
DIGEST_SECTIONS: list[tuple[str, str, str]] = [
    ("business", "What the company does",
     "What is the company's core business, and what are its main products, "
     "services and revenue sources?"),
    ("performance", "How the last fiscal year went",
     "How did revenue and profitability develop in the most recent fiscal "
     "year, and what drove the performance?"),
    ("risks", "Key risks",
     "What are the most significant risk factors the company discloses?"),
    ("management", "What management emphasizes",
     "What does management emphasize about strategy, investments and outlook "
     "in the management discussion and analysis?"),
]

# A digest spends this many pipeline runs from the daily budget.
DIGEST_QUERY_COST = len(DIGEST_SECTIONS)


def generate_digest(
    ticker: str,
    settings: Settings | None = None,
    progress: Progress = _noop,
    parallel: bool = True,
) -> dict:
    """Build the digest. Sections fail soft; figures may be empty (no XBRL)."""

    settings = settings or get_settings()
    ticker = ticker.strip().upper()
    started = time.monotonic()

    progress("Key figures (official filing data)…")
    try:
        figures = execute_numeric(FIGURE_QUERIES, ticker, settings=settings)
    except Exception:  # noqa: BLE001 — figures are additive, never blocking
        figures = []

    sections: dict[str, dict] = {}

    def run_section(key: str, title: str, question: str) -> tuple[str, dict]:
        try:
            report = answer_question(
                question, ticker, settings=settings,
                query_id=f"digest_{ticker.lower()}_{key}",
            )
            return key, {"key": key, "title": title, "question": question,
                         "report": report}
        except Exception as exc:  # noqa: BLE001 — one section ≠ whole digest
            return key, {"key": key, "title": title, "question": question,
                         "error": str(exc)}

    if parallel:
        progress(f"Running {len(DIGEST_SECTIONS)} sections in parallel…")
        with ThreadPoolExecutor(max_workers=len(DIGEST_SECTIONS)) as pool:
            futures = [pool.submit(run_section, *s) for s in DIGEST_SECTIONS]
            for fut in as_completed(futures):
                key, result = fut.result()
                sections[key] = result
                progress(f"  section ready: {result['title']}")
    else:  # pragma: no cover - debugging path
        for s in DIGEST_SECTIONS:
            key, result = run_section(*s)
            sections[key] = result

    ordered = [sections[k] for k, _, _ in DIGEST_SECTIONS]
    digest = {
        "ticker": ticker,
        "figures": figures,
        "sections": ordered,
        "latency_s": round(time.monotonic() - started, 1),
    }
    log_event(
        "digest_generated", settings.log_path,
        ticker=ticker, latency_s=digest["latency_s"],
        figures=len(figures),
        sections_ok=sum(1 for s in ordered if "report" in s),
        sections_failed=sum(1 for s in ordered if "error" in s),
    )
    return digest


def digest_summary(digest: dict) -> str:
    """Compact text used as chat-history context for follow-up questions."""

    parts = [r["text"] for r in digest.get("figures", [])[:3]]
    for s in digest.get("sections", []):
        report = s.get("report") or {}
        if report.get("summary"):
            parts.append(f"{s['title']}: {report['summary'][:200]}")
    return " | ".join(parts)


if __name__ == "__main__":  # pragma: no cover - operational entry point
    d = generate_digest(sys.argv[1] if len(sys.argv) > 1 else "AAPL",
                        progress=print)
    print(f"\n=== {d['ticker']} digest ({d['latency_s']}s) ===")
    for r in d["figures"]:
        print(f"🔢 {r['text']}")
    for s in d["sections"]:
        print(f"\n## {s['title']}")
        if "error" in s:
            print(f"  (failed: {s['error'][:80]})")
            continue
        rep = s["report"]
        print(f"  {rep['summary'][:300]}")
        print(f"  claims={len(rep['claims'])} "
              f"unsupported={rep['unsupported_rate']:.0%}")
