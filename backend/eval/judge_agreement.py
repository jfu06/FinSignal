"""Inter-judge agreement study: Claude judge vs OpenAI judge on IDENTICAL claims.

Swapping the judge swaps the measuring instrument — gate numbers from
different judges are not directly comparable. Before adopting a cross-vendor
judge, measure how often the two judges agree on the SAME generations:

  for each golden-set narrative case:
      retrieve + generate ONCE            (shared, so judges see identical claims)
      judge with anthropic  -> verdicts A
      judge with openai     -> verdicts B
  agreement = fraction of claims where A == B; disagreements listed for
  human arbitration (the golden set's human labels are the tie-breaker).

Usage (from backend/):
    python -m eval.judge_agreement          # all narrative cases
    python -m eval.judge_agreement 8        # first N narrative cases
"""

from __future__ import annotations

import copy
import dataclasses
import sys

from app.config import get_settings
from app.generation import generate_answer
from app.logging_utils import log_event
from app.refinement import retrieve_refined
from app.verification import verify_claims_batch
from eval.run_eval import load_cases


def main(limit: int | None = None) -> int:
    base = get_settings()
    judge_a = dataclasses.replace(base, judge_provider="anthropic")
    judge_b = dataclasses.replace(base, judge_provider="openai")

    cases = [c for c in load_cases() if c["kind"] == "narrative"]
    if limit:
        cases = cases[:limit]
    print(f"Inter-judge agreement over {len(cases)} cases "
          f"(anthropic:{judge_a.judge_model or judge_a.llm_model} vs "
          f"openai:{judge_b.judge_model or 'gpt-5-mini'})\n")

    total = agree = 0
    disagreements: list[dict] = []
    for case in cases:
        qid = f"agree_{case['case_id']}"
        chunks, _ = retrieve_refined(case["question"], case["ticker"],
                                     settings=base, query_id=qid)
        chunks_by_id = {c.chunk_id: c for c in chunks}
        _, claims = generate_answer(qid, case["question"], chunks, settings=base)
        if not claims:
            print(f"  {case['case_id']}: no claims generated, skipped")
            continue

        judged_a = verify_claims_batch(copy.deepcopy(claims), chunks_by_id, judge_a)
        judged_b = verify_claims_batch(copy.deepcopy(claims), chunks_by_id, judge_b)

        case_agree = 0
        for ca, cb in zip(judged_a, judged_b):
            total += 1
            if ca.verdict == cb.verdict:
                agree += 1
                case_agree += 1
            else:
                disagreements.append({
                    "case": case["case_id"],
                    "claim": ca.text[:100],
                    "anthropic": (ca.verdict.value, (ca.judge_reason or "")[:90]),
                    "openai": (cb.verdict.value, (cb.judge_reason or "")[:90]),
                })
        print(f"  {case['case_id']}: {case_agree}/{len(claims)} agree")

    rate = agree / total if total else 0.0
    print(f"\nAGREEMENT: {agree}/{total} claims = {rate:.1%}")
    if disagreements:
        print("\nDisagreements (human arbitration candidates):")
        for d in disagreements:
            print(f"  [{d['case']}] {d['claim']}")
            print(f"      anthropic: {d['anthropic'][0]} — {d['anthropic'][1]}")
            print(f"      openai:    {d['openai'][0]} — {d['openai'][1]}")

    log_event("judge_agreement", base.log_path,
              cases=len(cases), claims=total, agree=agree, rate=rate,
              disagreements=len(disagreements))
    return 0 if rate >= 0.85 else 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else None))
