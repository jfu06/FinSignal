"""Query-time orchestration as a LangGraph StateGraph (design-doc §3).

The pipeline is a compiled graph with explicit nodes and edges:

    START → boundary_check ─┬→ END                    (numeric question)
                            └→ retrieve → generate → verify ─┬→ assemble → END
                                              ↑               │
                                              └ flag_regen ←──┘ (CONTRADICTED,
                                                                 first time only)

Every node is a plain function over a typed state dict, so each stage stays
independently testable and the regeneration loop is an explicit conditional
edge instead of buried control flow.

Phase-1 boundaries:
- Numeric/aggregation questions are NOT routed to a metrics layer (that is
  Phase 2). A conservative keyword guard returns an explicit "not yet
  supported" reply instead — this is a data-boundary notice, not the Phase-2
  router.
- If the judge marks any claim CONTRADICTED, the graph loops back to
  generate ONCE with the judge's feedback, re-verifies (again one batch
  call), and keeps the attempt whose claims survive. Still-contradicted
  claims after the retry are blocked by the assembler.
"""

from __future__ import annotations

import json
import re
import sys
import time
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .assembler import DISCLAIMER, assemble_report
from .config import Settings, get_settings
from .db import connect
from .generation import generate_answer
from .logging_utils import log_event
from .models import Chunk, Claim, Verdict
from .edgar import EdgarError, resolve_ticker
from .refinement import RefinementTrace, retrieve_refined
from .risk_signals import annotate_risk_claims
from .router import execute_numeric, route_question
from .verification import verify_claims_batch

# Conservative guard for explicitly computational/aggregation asks.
# Deliberately narrow: "营收增长主要靠什么驱动" is narrative and must NOT match.
_NUMERIC_PATTERNS = [
    r"CAGR", r"复合增长率", r"年均增长",
    r"平均", r"\baverage\b", r"\bmean\b", r"\bmedian\b", r"中位数",
    r"环比", r"合计", r"总和", r"\bsum\b",
    r"计算", r"算一下", r"\bcalculate\b", r"\bcompute\b",
    r"增长率是多少", r"增长了百分之多少", r"growth rate", r"percent(?:age)? change",
    r"多少倍", r"比率是多少", r"\bratio\b", r"利润率是多少", r"毛利率是多少",
]
_NUMERIC_RE = re.compile("|".join(_NUMERIC_PATTERNS), re.IGNORECASE)

# Coverage/enumeration questions: the answer is a LIST whose quality is
# breadth ("biggest risk factors", "main competitors", "all segments").
# Flat top-k retrieval optimizes similarity, not coverage — these get a
# doubled, MMR-diversified retrieval and an honest no-ranking note.
_COVERAGE_PATTERNS = [
    r"risk factors?", r"\brisks\b", r"风险",
    r"competitors?|competition|competitive", r"竞争",
    r"segments?|business lines?|product lines?", r"板块|业务线",
    r"what are (?:the |its |their )?(?:biggest|largest|main|key|top|major|primary)",
    r"list (?:the|all|its)\b", r"最大的|主要的|哪些",
]
_COVERAGE_RE = re.compile("|".join(_COVERAGE_PATTERNS), re.IGNORECASE)

COVERAGE_NOTE = (
    "10-K filings list items like risks without ranking them by severity — "
    "the points below reflect the filing's coverage, in no particular order."
)


def is_coverage_question(question: str) -> bool:
    """True for enumeration asks whose answer quality is breadth."""

    return bool(_COVERAGE_RE.search(question))

class PipelineError(ValueError):
    """Invalid user input (unknown ticker, empty question, …)."""


class PipelineState(TypedDict, total=False):
    """State carried between graph nodes."""

    question: str
    ticker: str
    query_id: str
    doc_id: str                    # benchmark/oracle-document mode: pin retrieval
    numeric_scope: dict            # oracle-doc numeric: {"cik", "accn"}
    route: str                     # narrative | numeric | hybrid
    coverage: bool                 # enumeration ask -> diversified retrieval
    needs_onboarding: str          # ticker mentioned but not in the corpus
    numeric_queries: list[dict]
    numeric_results: list[dict]
    chunks: list[Chunk]
    refinement: RefinementTrace
    summary: str
    claims: list[Claim]
    regenerated: bool
    feedback: str
    report: dict[str, Any]


def is_numeric_question(question: str) -> bool:
    """True if the question explicitly asks for computation/aggregation."""

    return bool(_NUMERIC_RE.search(question))


def _xbrl_rescue_flags(report: dict, ticker: str,
                       settings: Settings) -> None:
    """Second numeric channel: figures absent from the cited text but equal
    to an official XBRL fact of this company are verified, not flagged.

    (A claim often quotes the company-wide total while citing the excerpt
    that EXPLAINS it — the table lives in a neighboring chunk.)
    """

    flagged = [c for c in report.get("claims", [])
               if (c.get("span_overlap") or {}).get("flagged")]
    if not flagged:
        return
    try:
        from .edgar import resolve_ticker
        from .span_overlap import values_match_facts

        cik = int(resolve_ticker(ticker)["cik"])
        with connect(settings) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT val FROM xbrl_facts WHERE cik = %s", (cik,))
            facts = [float(r[0]) for r in cur.fetchall()]
        for c in flagged:
            if values_match_facts(c["text"], facts):
                c["span_overlap"]["flagged"] = False
                c["span_overlap"]["xbrl_match"] = True
    except Exception:  # noqa: BLE001 — rescue channel, never critical
        pass


def _known_tickers(settings: Settings) -> set[str]:
    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT ticker FROM chunks WHERE corpus = 'live'")
        return {r[0] for r in cur.fetchall()}


def _persist_claims(claims: list[Claim], settings: Settings) -> None:
    if not claims:
        return
    with connect(settings) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO claims
                (claim_id, query_id, text, cited_chunk_ids, verdict, judge_reason)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (claim_id) DO UPDATE SET
                text = EXCLUDED.text,
                cited_chunk_ids = EXCLUDED.cited_chunk_ids,
                verdict = EXCLUDED.verdict,
                judge_reason = EXCLUDED.judge_reason
            """,
            [
                (
                    c.claim_id,
                    c.query_id,
                    c.text,
                    c.cited_chunk_ids,
                    c.verdict.value if c.verdict else None,
                    c.judge_reason,
                )
                for c in claims
            ],
        )
        conn.commit()


def _contradiction_feedback(claims: list[Claim]) -> str:
    lines = [
        f"- {c.text!r}: {c.judge_reason}"
        for c in claims
        if c.verdict is Verdict.CONTRADICTED
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Graph construction: one node per pipeline stage, edges = the design-doc flow.
# Nodes close over `settings` but call the module-level stage functions, so
# tests can monkeypatch individual stages without touching the graph.
# --------------------------------------------------------------------------


def build_graph(settings: Settings):
    """Compile the QA pipeline as a LangGraph StateGraph."""

    def route_node(state: PipelineState) -> PipelineState:
        # Oracle-document mode (external benchmark): the document is given, so
        # company detection never applies. Numeric routing runs only when a
        # numeric_scope (cik + accession) is provided — the XBRL layer then
        # answers with figures as printed in that exact filing.
        if state.get("doc_id"):
            if not state.get("numeric_scope"):
                return {"route": "narrative", "numeric_queries": [],
                        "needs_onboarding": ""}
            decision = route_question(
                state["question"], state["ticker"],
                settings=settings, query_id=state["query_id"],
            )
            return {"route": decision["route"],
                    "numeric_queries": decision["queries"],
                    "needs_onboarding": ""}
        # Phase-2 router: numeric -> metrics layer, narrative -> RAG,
        # hybrid -> both. Fails open to narrative inside route_question.
        decision = route_question(
            state["question"], state["ticker"],
            settings=settings, query_id=state["query_id"],
        )
        # Free-form company detection: a company mentioned in the question
        # beats the sidebar/default ticker (FinChat-style UX). If the default
        # ticker is itself among the mentions, keep it (e.g. comparisons).
        resolved = state["ticker"]
        needs = ""
        mentioned = decision.get("companies") or []
        if mentioned and resolved not in mentioned:
            known = _known_tickers(settings)
            in_corpus = next((c for c in mentioned if c in known), None)
            if in_corpus:
                resolved = in_corpus
            else:
                candidate = mentioned[0]
                try:
                    resolve_ticker(candidate)  # real US listing?
                    needs = candidate          # valid but not onboarded yet
                except EdgarError:
                    pass                       # hallucinated -> keep default
        log_event(
            "routed", settings.log_path,
            query_id=state["query_id"], route=decision["route"],
            queries=decision["queries"], companies=mentioned,
            resolved_ticker=resolved, needs_onboarding=needs or None,
        )
        return {"route": decision["route"],
                "numeric_queries": decision["queries"],
                "ticker": resolved,
                "needs_onboarding": needs}

    def onboarding_required(state: PipelineState) -> PipelineState:
        tk = state["needs_onboarding"]
        return {"report": {
            "query_id": state["query_id"],
            "ticker": tk,
            "question": state["question"],
            "needs_onboarding": tk,
            "message": (
                f"{tk} isn't in the corpus yet. Add it (about a minute) and "
                f"I'll answer from its latest 10-K."
            ),
        }}

    def numeric_node(state: PipelineState) -> PipelineState:
        results = execute_numeric(
            state["numeric_queries"], state["ticker"], settings=settings,
            scope=state.get("numeric_scope") or None)
        update: PipelineState = {"numeric_results": results}
        if state["route"] == "numeric" and not results:
            # Nothing computable (registry gap, missing data): fall back to
            # the RAG line rather than refusing.
            log_event("numeric_fallback", settings.log_path,
                      query_id=state["query_id"])
            update["route"] = "narrative"
        return update

    def assemble_numeric(state: PipelineState) -> PipelineState:
        # Pure-numeric answer: deterministic renderings, no LLM, no claims.
        results = state["numeric_results"]
        report: dict[str, Any] = {
            "query_id": state["query_id"],
            "ticker": state["ticker"],
            "question": state["question"],
            "summary": "\n".join(r["text"] for r in results),
            "claims": [], "risk_flags": [], "unsupported_claims": [],
            "blocked_claims": [], "unsupported_rate": 0.0,
            "numeric": results,
            "disclaimer": DISCLAIMER,
        }
        log_event(
            "report_summary", settings.log_path,
            query_id=state["query_id"], unsupported_rate=0.0,
            num_claims=0, num_blocked=0, numeric_results=len(results),
        )
        return {"report": report}

    def retrieve(state: PipelineState) -> PipelineState:
        # Bounded agentic refinement loop (see app.refinement).
        coverage = is_coverage_question(state["question"])
        chunks, trace = retrieve_refined(
            state["question"], state["ticker"],
            settings=settings, query_id=state["query_id"],
            doc_id=state.get("doc_id") or None,
            coverage=coverage,
        )
        return {"chunks": chunks, "refinement": trace, "coverage": coverage}

    def generate(state: PipelineState) -> PipelineState:
        # Second pass gets an "r" suffix so both attempts stay on record.
        gen_id = state["query_id"] + ("r" if state.get("regenerated") else "")
        summary, claims = generate_answer(
            gen_id, state["question"], state["chunks"],
            settings=settings, feedback=state.get("feedback"),
        )
        return {"summary": summary, "claims": claims}

    def verify(state: PipelineState) -> PipelineState:
        # ONE batch judge call per attempt; verdicts persisted immediately.
        chunks_by_id = {c.chunk_id: c for c in state["chunks"]}
        claims = verify_claims_batch(state["claims"], chunks_by_id, settings=settings)
        _persist_claims(claims, settings)
        return {"claims": claims}

    def flag_regeneration(state: PipelineState) -> PipelineState:
        # CONTRADICTED -> ERROR: record + arm the single regeneration pass.
        log_event(
            "regeneration_triggered", settings.log_path,
            query_id=state["query_id"],
            contradicted_claim_ids=[
                c.claim_id for c in state["claims"]
                if c.verdict is Verdict.CONTRADICTED
            ],
        )
        return {
            "regenerated": True,
            "feedback": _contradiction_feedback(state["claims"]),
        }

    def assemble(state: PipelineState) -> PipelineState:
        report = assemble_report(
            state["query_id"], state["question"], state["ticker"],
            state["summary"], state["claims"], state["chunks"],
        )
        _xbrl_rescue_flags(report, state["ticker"], settings)
        report["retrieved_chunk_ids"] = [c.chunk_id for c in state["chunks"]]
        if state.get("coverage"):
            report["coverage_note"] = COVERAGE_NOTE
            # Materiality signals (quantified / realized / echoed) — see
            # app.risk_signals. Deterministic document facts, no ranking.
            try:
                annotate_risk_claims(
                    report, state["claims"],
                    {c.chunk_id: c for c in state["chunks"]},
                    state["ticker"], settings=settings,
                    doc_id=state.get("doc_id") or None,
                )
            except Exception:  # noqa: BLE001 — decoration, never critical
                pass
        if state.get("numeric_results"):  # hybrid: verified figures alongside
            report["numeric"] = state["numeric_results"]
        trace = state["refinement"]
        report["retrieval"] = {
            "rounds": trace.rounds,
            "decisions": trace.decisions,
            "final_query": trace.final_query,
            "final_k": trace.final_k,
        }
        log_event(
            "report_summary", settings.log_path,
            query_id=state["query_id"],
            unsupported_rate=report["unsupported_rate"],
            num_claims=len(state["claims"]),
            num_blocked=len(report["blocked_claims"]),
        )
        return {"report": report}

    def route_after_router(state: PipelineState) -> str:
        if state.get("needs_onboarding"):
            return "onboard"
        return "narrative" if state["route"] == "narrative" else "numeric"

    def route_after_numeric(state: PipelineState) -> str:
        # hybrid (or numeric-with-no-results fallback) continues into RAG
        return "assemble_numeric" if state["route"] == "numeric" else "narrative"

    def route_after_verify(state: PipelineState) -> str:
        contradicted = any(
            c.verdict is Verdict.CONTRADICTED for c in state["claims"]
        )
        if contradicted and not state.get("regenerated"):
            return "regenerate"
        return "assemble"

    graph = StateGraph(PipelineState)
    graph.add_node("route_question", route_node)
    graph.add_node("onboarding_required", onboarding_required)
    graph.add_node("run_numeric", numeric_node)
    graph.add_node("assemble_numeric", assemble_numeric)
    graph.add_node("retrieve", retrieve)
    graph.add_node("generate", generate)
    graph.add_node("verify", verify)
    graph.add_node("flag_regeneration", flag_regeneration)
    graph.add_node("assemble", assemble)

    graph.add_edge(START, "route_question")
    graph.add_conditional_edges(
        "route_question", route_after_router,
        {"narrative": "retrieve", "numeric": "run_numeric",
         "onboard": "onboarding_required"},
    )
    graph.add_edge("onboarding_required", END)
    graph.add_conditional_edges(
        "run_numeric", route_after_numeric,
        {"assemble_numeric": "assemble_numeric", "narrative": "retrieve"},
    )
    graph.add_edge("assemble_numeric", END)
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", "verify")
    graph.add_conditional_edges(
        "verify", route_after_verify,
        {"regenerate": "flag_regeneration", "assemble": "assemble"},
    )
    graph.add_edge("flag_regeneration", "generate")  # the one-shot retry loop
    graph.add_edge("assemble", END)
    return graph.compile()


def answer_question(
    question: str,
    ticker: str,
    settings: Settings | None = None,
    query_id: str | None = None,
    doc_id: str | None = None,
    numeric_scope: dict | None = None,
) -> dict:
    """Run the full RAG pipeline graph for one question; returns the report.

    ``doc_id`` switches on oracle-document mode (FinanceBench): retrieval is
    pinned to that document, company detection is bypassed, and the ticker is
    display-only (no live-corpus membership check). ``numeric_scope``
    ({"cik", "accn"}) additionally enables the XBRL numeric layer in this
    mode, restricted to figures as printed in that exact filing; without it
    everything routes narrative.
    """

    settings = settings or get_settings()
    query_id = query_id or f"q{uuid.uuid4().hex[:8]}"

    # --- input validation (production-quality requirement) ---
    if not question or not question.strip():
        raise PipelineError("question must be a non-empty string")
    question = question.strip()
    ticker = (ticker or "").strip().upper()
    if doc_id is None:
        known = _known_tickers(settings)
        if ticker not in known:
            raise PipelineError(
                f"Unknown ticker {ticker!r}. Ingested tickers: {sorted(known)}"
            )

    log_event(
        "query_received", settings.log_path,
        query_id=query_id, ticker=ticker, question=question,
        doc_id=doc_id,
    )

    started = time.monotonic()
    graph = build_graph(settings)
    final_state = graph.invoke(
        {"question": question, "ticker": ticker, "query_id": query_id,
         "doc_id": doc_id or "", "numeric_scope": numeric_scope or {}}
    )
    report = final_state["report"]
    report["latency_s"] = round(time.monotonic() - started, 1)
    return report


def _print_report(report: dict) -> None:  # pragma: no cover - CLI rendering
    if not report.get("supported", True):
        print(f"\n⛔ {report['message']}")
        return
    if report.get("numeric"):
        print("\n### 数值(官方申报数据,确定性计算)")
        for r in report["numeric"]:
            print(f"🔢 {r['text']}")
            for s in r["sources"][:2]:
                print(f"    ↳ {s['form']} {s['accn']}  {s['url']}")
    print(f"\n### 摘要\n{report['summary']}\n")
    print("### 结论")
    for c in report["claims"]:
        mark = "⚠️ [未验证]" if c["unverified"] else "✅"
        cites = ", ".join(x["chunk_id"] for x in c["citations"]) or "无引用"
        print(f"{mark} ({c['credibility']:.1f}) {c['text']}")
        print(f"    ↳ 引用: {cites}")
    if report["risk_flags"]:
        print("\n### 风险提示")
        for r in report["risk_flags"]:
            print(f"- {r}")
    if report["unsupported_claims"]:
        print("\n### 未支持结论清单")
        for u in report["unsupported_claims"]:
            print(f"- {u['text']}  (原因: {u['reason']})")
    if report["blocked_claims"]:
        print(f"\n(已拦截 {len(report['blocked_claims'])} 条与原文矛盾的结论,不予展示)")
    retr = report.get("retrieval", {})
    if retr.get("rounds", 1) > 1:
        print(f"\n(检索经过 {retr['rounds']} 轮优化: {' → '.join(retr['decisions'])}"
              f"; 最终查询: {retr['final_query'][:80]!r}, k={retr['final_k']})")
    print(f"\n无依据结论率: {report['unsupported_rate']:.1%}")
    print(f"\n{report['disclaimer']}")


if __name__ == "__main__":  # pragma: no cover - operational entry point
    if len(sys.argv) < 3:
        raise SystemExit('usage: python -m app.pipeline "<question>" <TICKER> [out.json]')
    result = answer_question(sys.argv[1], sys.argv[2])
    _print_report(result)
    if len(sys.argv) > 3:
        with open(sys.argv[3], "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
        print(f"\n[report exported to {sys.argv[3]}]")
