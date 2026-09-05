"""LLM cost accounting from the jsonl event log — no external monitoring stack.

Every ``llm_call`` event now records real ``input_tokens``/``output_tokens``
from the API response. This module aggregates them into an estimated-cost
report by day × stage × model. Authoritative spend lives in the Anthropic
console; this report answers "which part of MY pipeline is spending it".

Prices are per million tokens and env-overridable::

    PRICE_<MODEL_PREFIX>_IN / PRICE_<MODEL_PREFIX>_OUT   (USD per MTok)

Usage:
    python -m app.costs            # full history
    python -m app.costs 2026-08-13 # one day
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

# Approximate per-MTok USD prices; override via env if your rates differ.
_DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (1.0, 5.0),
    "claude-opus": (15.0, 75.0),
    "gpt-5-mini": (0.25, 2.0),
    "gpt-5": (1.25, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4o": (2.5, 10.0),
}


def price_for(model: str) -> tuple[float, float]:
    """(input, output) USD per MTok for a model id, by longest prefix match.

    Accepts provider-qualified ids like ``openai:gpt-5-mini``.
    """

    model = (model or "").lower().split(":", 1)[-1]
    for prefix, (pin, pout) in sorted(_DEFAULT_PRICES.items(),
                                      key=lambda kv: -len(kv[0])):
        if model.startswith(prefix):
            env_key = prefix.upper().replace("-", "_")
            return (
                float(os.getenv(f"PRICE_{env_key}_IN", pin)),
                float(os.getenv(f"PRICE_{env_key}_OUT", pout)),
            )
    return (3.0, 15.0)  # unknown model: assume mid-tier


def aggregate(log_path: str | Path, day: str | None = None) -> list[dict]:
    """Rows of {day, stage, model, calls, input_tokens, output_tokens, cost}."""

    rows: dict[tuple, dict] = defaultdict(
        lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    )
    path = Path(log_path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if '"event": "llm_call"' not in line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            d = (r.get("ts") or "")[:10]
            if day and d != day:
                continue
            key = (d, r.get("stage", "?"), r.get("model", "?"))
            agg = rows[key]
            agg["calls"] += 1
            agg["input_tokens"] += int(r.get("input_tokens") or 0)
            agg["output_tokens"] += int(r.get("output_tokens") or 0)

    out = []
    for (d, stage, model), agg in sorted(rows.items()):
        pin, pout = price_for(model)
        cost = (agg["input_tokens"] * pin + agg["output_tokens"] * pout) / 1e6
        out.append({"day": d, "stage": stage, "model": model, **agg,
                    "est_cost_usd": round(cost, 4)})
    return out


def main() -> None:  # pragma: no cover - CLI rendering
    from .config import get_settings

    day = sys.argv[1] if len(sys.argv) > 1 else None
    rows = aggregate(get_settings().log_path, day)
    if not rows:
        print("No llm_call events with token usage found "
              "(usage logging starts with this version).")
        return
    total = 0.0
    print(f"{'day':<12}{'stage':<12}{'model':<28}{'calls':>6}"
          f"{'in_tok':>10}{'out_tok':>9}{'est_$':>8}")
    for r in rows:
        total += r["est_cost_usd"]
        print(f"{r['day']:<12}{r['stage']:<12}{r['model']:<28}{r['calls']:>6}"
              f"{r['input_tokens']:>10,}{r['output_tokens']:>9,}"
              f"{r['est_cost_usd']:>8.3f}")
    print(f"\nTOTAL estimated: ${total:.2f}  "
          f"(prices are approximations — the Anthropic console is authoritative)")


if __name__ == "__main__":  # pragma: no cover
    main()
