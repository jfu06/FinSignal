"""Release-gate evaluation over the mini golden set (design-doc §5).

Runs the FULL pipeline on every golden-set case, reports the judge verdict
distribution and the overall unsupported rate, prints concrete failure
examples, and **exits non-zero when the unsupported rate exceeds the 4%
threshold** (UNSUPPORTED_RATE_THRESHOLD) — CI-ready release-gate semantics,
run manually this phase.

Also syncs the golden set into the ``test_cases`` table (design-doc §7).

Usage (from backend/):
    python -m eval.run_eval
"""

from __future__ import annotations

import json
import sys
import os
from collections import Counter
from pathlib import Path

from app.config import get_settings
from app.db import connect
from app.logging_utils import log_event
from app.pipeline import answer_question

GOLDEN_SET = Path(__file__).resolve().parent / "golden_set.json"


# Second-axis gate: fraction of displayed claims that are generic filing
# language ("success depends on innovation") rather than company-specific
# answers. Provisional threshold — calibrate after the first baselines.
BOILERPLATE_GATE = float(os.getenv("BOILERPLATE_GATE", "0.25"))


def load_cases() -> list[dict]:
    data = json.loads(GOLDEN_SET.read_text(encoding="utf-8"))
    return data["cases"]


def sync_test_cases(cases: list[dict], settings) -> None:  # noqa: ANN001
    """Upsert golden-set cases into the test_cases table."""

    with connect(settings) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO test_cases
                (case_id, ticker, question, expected_answer_snippet, expected_chunk_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (case_id) DO UPDATE SET
                ticker = EXCLUDED.ticker,
                question = EXCLUDED.question,
                expected_answer_snippet = EXCLUDED.expected_answer_snippet,
                expected_chunk_id = EXCLUDED.expected_chunk_id
            """,
            [
                (
                    c["case_id"], c["ticker"], c["question"],
                    c.get("expected_answer_snippet"), c.get("expected_chunk_id"),
                )
                for c in cases
            ],
        )
        conn.commit()


def check_numeric_case(case: dict, report: dict) -> dict:
    """Pure check: was the numeric answer COMPUTED CORRECTLY?

    Correct = numeric results present, every expected substring appears in
    the rendered texts, and (when pinned) the expected accession number is
    among the sources. This is what catches a broken formula, a broken dedup
    rule, or a broken metric mapping.
    """

    results = report.get("numeric") or []
    joined = " ".join(r.get("text", "") for r in results)
    accns = {s.get("accn") for r in results for s in r.get("sources", [])}
    subs_ok = all(s in joined for s in case.get("expected_substrings", []))
    accn = case.get("expected_accn")
    accn_ok = (accn is None) or (accn in accns)
    return {
        "numeric_answered": bool(results),
        "values_ok": bool(results) and subs_ok,
        "accn_ok": accn_ok,
        "ok": bool(results) and subs_ok and accn_ok,
    }


def evaluate_case(case: dict, settings) -> dict:  # noqa: ANN001
    """Run one case through the pipeline; return per-case metrics."""

    report = answer_question(
        case["question"], case["ticker"],
        settings=settings, query_id=f"eval_{case['case_id']}",
    )

    if case["kind"] == "numeric":
        check = check_numeric_case(case, report)
        return {"case_id": case["case_id"], "kind": case["kind"],
                **check, "verdicts": Counter(), "n_claims": 0}

    if case["kind"] == "numeric_boundary":
        # Phase 2: numeric questions must be HANDLED, not rejected — either a
        # deterministic numeric answer, or a graceful fallback to a judged
        # narrative answer. A crash or an unhandled shape fails.
        ok = bool(report.get("numeric")) or bool(report.get("claims"))
        return {"case_id": case["case_id"], "kind": case["kind"],
                "boundary_ok": ok,
                "numeric_answered": bool(report.get("numeric")),
                "verdicts": Counter(), "n_claims": 0}

    # Narrative: gather final verdicts (displayed + blocked = all judged claims).
    verdicts: Counter = Counter()
    for claim in report["claims"]:
        verdicts[claim["verdict"]] += 1
    for blocked in report["blocked_claims"]:
        verdicts[blocked["verdict"]] += 1

    # Span-overlap second signal (displayed claims carry it).
    span_flagged = [
        c["claim_id"] for c in report["claims"]
        if c.get("span_overlap", {}).get("flagged")
    ]
    number_scores = [
        c["span_overlap"]["number_match"] for c in report["claims"]
        if c.get("span_overlap", {}).get("number_match") is not None
    ]

    retrieved = report.get("retrieved_chunk_ids", [])
    retrieval_hit = case["expected_chunk_id"] in retrieved

    snippet = case.get("expected_answer_snippet")
    answer_text = report["summary"] + " ".join(c["text"] for c in report["claims"])
    snippet_hit = bool(snippet) and snippet.lower() in answer_text.lower()

    failures = [
        {"claim": c["text"][:120], "verdict": c["verdict"],
         "reason": (c.get("judge_reason") or "")[:120]}
        for c in report["claims"] if c["verdict"] != "SUPPORTED"
    ] + [
        {"claim": f"(blocked) {b['claim_id']}", "verdict": b["verdict"],
         "reason": (b.get("reason") or "")[:120]}
        for b in report["blocked_claims"]
    ]

    return {
        "case_id": case["case_id"], "kind": case["kind"],
        "verdicts": verdicts, "n_claims": sum(verdicts.values()),
        "retrieval_hit": retrieval_hit, "snippet_hit": snippet_hit,
        "span_flagged": span_flagged, "number_scores": number_scores,
        "claim_texts": [c["text"] for c in report["claims"]],
        "failures": failures,
    }


def main() -> int:
    settings = get_settings()
    cases = load_cases()
    sync_test_cases(cases, settings)
    print(f"Golden set: {len(cases)} cases (threshold: "
          f"{settings.unsupported_rate_threshold:.0%} unsupported rate)\n")

    results = []
    for case in cases:
        print(f"→ {case['case_id']} [{case['ticker']}] {case['question'][:60]}")
        try:
            results.append(evaluate_case(case, settings))
        except Exception as exc:  # noqa: BLE001 — one bad case must not kill the run
            print(f"  !! case crashed ({exc}); retrying once")
            try:
                results.append(evaluate_case(case, settings))
            except Exception as exc2:  # noqa: BLE001
                print(f"  !! retry also crashed: {exc2}")
                results.append({"case_id": case["case_id"], "kind": case["kind"],
                                "error": str(exc2), "verdicts": Counter(),
                                "n_claims": 0})

    # ---- aggregate ----
    total_verdicts: Counter = Counter()
    for r in results:
        total_verdicts.update(r["verdicts"])
    total_claims = sum(total_verdicts.values())
    unsupported = total_verdicts["NOT_ENOUGH_INFO"] + total_verdicts["CONTRADICTED"]
    rate = unsupported / total_claims if total_claims else 0.0

    narrative = [r for r in results if r["kind"] == "narrative"]
    boundary = [r for r in results if r["kind"] == "numeric_boundary"]
    numeric = [r for r in results if r["kind"] == "numeric"]
    crashed = [r for r in results if "error" in r]

    print("\n" + "=" * 62)
    print("Per-case results")
    print("=" * 62)
    for r in narrative:
        if "error" in r:
            print(f"  {r['case_id']}: CRASHED — {r['error'][:70]}")
            continue
        v = r["verdicts"]
        print(f"  {r['case_id']}: claims={r['n_claims']} "
              f"S={v['SUPPORTED']} NEI={v['NOT_ENOUGH_INFO']} "
              f"C={v['CONTRADICTED']} "
              f"retrieval_hit={'Y' if r['retrieval_hit'] else 'n'} "
              f"snippet_hit={'Y' if r['snippet_hit'] else 'n'}")
    for r in boundary:
        status = "OK" if r.get("boundary_ok") else "FAILED"
        how = "numeric" if r.get("numeric_answered") else "narrative-fallback"
        print(f"  {r['case_id']}: numeric question handled ({how}) {status}")
    for r in numeric:
        status = "OK" if r.get("ok") else "WRONG"
        detail = ("" if r.get("ok") else
                  f" (answered={r.get('numeric_answered')}, "
                  f"values_ok={r.get('values_ok')}, accn_ok={r.get('accn_ok')})")
        print(f"  {r['case_id']}: numeric value check {status}{detail}")

    print("\nVerdict distribution:", dict(total_verdicts))
    hits = sum(1 for r in narrative if r.get("retrieval_hit"))
    print(f"Retrieval hit rate: {hits}/{len(narrative)}")

    # Span-overlap second signal (deterministic, beside the LLM judge)
    all_flagged = [cid for r in narrative for cid in r.get("span_flagged", [])]
    all_scores = [s for r in narrative for s in r.get("number_scores", [])]
    if all_scores:
        print(f"Span-overlap: number-match avg {sum(all_scores)/len(all_scores):.2f} "
              f"over {len(all_scores)} numeric claims; "
              f"{len(all_flagged)} flagged (numbers not found in citations)")
        for cid in all_flagged[:5]:
            print(f"  ⚑ {cid}")
    print(f"UNSUPPORTED RATE: {rate:.2%}  "
          f"({unsupported}/{total_claims} claims; gate at "
          f"{settings.unsupported_rate_threshold:.0%})")

    # ---- second axis: informativeness (faithful boilerplate scores a
    # perfect 0% unsupported while answering nothing) ----
    from app.boilerplate import boilerplate_flags
    q_by_case = {c["case_id"]: c["question"] for c in cases}
    labeled = [(r["case_id"], text) for r in narrative
               for text in r.get("claim_texts", [])]
    bp_flags = boilerplate_flags(
        [text for _, text in labeled],
        settings=settings,
        questions=[q_by_case.get(cid, "") for cid, _ in labeled])
    bp_rate = (sum(bp_flags) / len(bp_flags)) if bp_flags else 0.0
    print(f"BOILERPLATE RATE: {bp_rate:.2%}  "
          f"({sum(bp_flags)}/{len(bp_flags)} claims generic; gate at "
          f"{BOILERPLATE_GATE:.0%})")
    for (case_id, text), flag in zip(labeled, bp_flags):
        if flag:
            print(f"  ▢ {case_id}: {text[:100]}")

    failures = [f for r in narrative for f in r.get("failures", [])]
    if failures:
        print("\nFailure examples:")
        for f in failures[:5]:
            print(f"  [{f['verdict']}] {f['claim']}")
            print(f"      reason: {f['reason']}")

    boundary_failed = [r for r in boundary if not r.get("boundary_ok")]
    numeric_wrong = [r for r in numeric if not r.get("ok")]
    if boundary_failed:
        print(f"\n⚠️  {len(boundary_failed)} numeric-boundary case(s) FAILED "
              f"(question not handled).")
    if numeric_wrong:
        print(f"⚠️  {len(numeric_wrong)} numeric case(s) computed WRONG values.")
    if crashed:
        print(f"⚠️  {len(crashed)} case(s) crashed.")

    log_event(
        "eval_summary", settings.log_path,
        num_cases=len(cases), total_claims=total_claims,
        verdicts=dict(total_verdicts), unsupported_rate=rate,
        boilerplate_rate=bp_rate,
        retrieval_hits=hits, boundary_failures=len(boundary_failed),
        crashed=len(crashed),
        span_flagged=len(all_flagged),
        number_match_avg=(sum(all_scores) / len(all_scores)) if all_scores else None,
    )

    # ---- release gate ----
    if rate > settings.unsupported_rate_threshold:
        print(f"\n❌ GATE FAILED: unsupported rate {rate:.2%} > "
              f"{settings.unsupported_rate_threshold:.0%} — do not ship.")
        return 1
    if numeric_wrong:
        print("\n❌ GATE FAILED: numeric answers computed wrong values — "
              "do not ship.")
        return 1
    if crashed:
        print("\n❌ GATE FAILED: eval could not run end-to-end.")
        return 1
    if bp_flags and bp_rate > BOILERPLATE_GATE:
        print(f"\n❌ GATE FAILED: boilerplate rate {bp_rate:.2%} > "
              f"{BOILERPLATE_GATE:.0%} — verified but uninformative answers.")
        return 1
    print("\n✅ GATE PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
