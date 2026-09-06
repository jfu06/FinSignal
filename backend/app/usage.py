"""Usage accounting for the public-deployment cost guardrails.

Counts queries against the existing jsonl event log — no new storage. Every
pipeline run already writes a ``query_received`` event with a UTC ``ts``, so
"queries today" is a scan of the log file. At demo scale (thousands of lines)
this is milliseconds; it deliberately errs on the conservative side by also
counting numeric-boundary rejections (which cost ~nothing) as spent budget.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def queries_today(log_path: str | Path, now: datetime | None = None) -> int:
    """Number of ``query_received`` events logged today (UTC)."""

    path = Path(log_path)
    if not path.exists():
        return 0
    day_prefix = f'{{"ts": "{(now or datetime.now(timezone.utc)).date().isoformat()}'
    count = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            # cheap string checks — no json parsing on the hot path
            if line.startswith(day_prefix) and '"event": "query_received"' in line:
                # Operator-initiated eval traffic (release gate: "eval_…",
                # FinanceBench: "fb_…") must not consume the visitor budget —
                # a local benchmark run would otherwise lock the demo out.
                if ('"query_id": "eval' in line
                        or '"query_id": "fb_' in line
                        or '"query_id": "smoke_' in line):
                    continue
                count += 1
    return count


def daily_budget_left(log_path: str | Path, budget: int) -> int:
    """Remaining queries in today's global budget (never negative)."""

    return max(0, budget - queries_today(log_path))


def visitor_queries_today(log_path: str | Path, visitor: str,
                          now: datetime | None = None) -> int:
    """Number of ``visitor_query`` events for this visitor today (UTC).

    ``visitor`` is a hashed identifier (never a raw IP) written by the UI
    at ask time — the per-person layer of the quota ("10 questions per
    person per day"), beneath the global spend backstop.
    """

    path = Path(log_path)
    if not path.exists():
        return 0
    day_prefix = f'{{"ts": "{(now or datetime.now(timezone.utc)).date().isoformat()}'
    needle = f'"visitor": "{visitor}"'
    count = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if (line.startswith(day_prefix)
                    and '"event": "visitor_query"' in line
                    and needle in line):
                count += 1
    return count
