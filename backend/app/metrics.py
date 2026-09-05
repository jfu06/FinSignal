"""Metrics layer: deterministic numeric answers over xbrl_facts (Phase 2).

NO LLM anywhere in this module. Numbers are selected by SQL + pure Python and
every returned value carries provenance (accession number -> SEC filing URL).
The LLM's only later job is phrasing sentences AROUND these verified numbers.

Conventions and pitfalls handled (docs/data-dictionary.md):
- Fiscal year label = calendar year containing the period END (AAPL FY2025
  ends 2025-09-27, MSFT FY2026 ends 2026-06-30, NVDA FY2026 ends Jan 2026 —
  all match issuer conventions).
- Annual points are duration 330-400 days; quarterly 60-120 days (52/53-week
  fiscal calendars, §5.7).
- Tag fallback per metric via the METRICS priority list (§5.1), applied
  PER-YEAR so stitched time series survive tag migrations.
- Instant (balance-sheet) metrics are anchored to the company's actual
  fiscal-year-end date, derived from a duration anchor metric (§5.5).
- Q4 single quarter is never reported and must be computed: FY − Q1 − Q2 − Q3
  (§5.6).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .config import Settings, get_settings
from .db import connect
from .edgar import resolve_ticker
from .xbrl import METRICS

ANNUAL_DAYS = (330, 400)
QUARTER_DAYS = (60, 120)


class MetricError(ValueError):
    """User-displayable metrics failure (unknown metric, no data, …)."""


@dataclass(frozen=True)
class Fact:
    """One deduplicated data point from facts_dedup."""

    start: date | None
    end: date
    val: float
    accn: str
    form: str
    filed: date


@dataclass(frozen=True)
class MetricPoint:
    """A verified numeric answer with provenance."""

    ticker: str
    metric: str
    fy: int
    value: float
    unit: str
    period_start: date | None
    period_end: date
    accn: str
    form: str
    source_url: str
    derived: bool = False          # True when computed (Q4, growth, margins)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker, "metric": self.metric, "fy": self.fy,
            "value": self.value, "unit": self.unit,
            "period_start": str(self.period_start) if self.period_start else None,
            "period_end": str(self.period_end),
            "accn": self.accn, "form": self.form, "source_url": self.source_url,
            "derived": self.derived, "note": self.note,
        }


def filing_url(cik: int, accn: str) -> str:
    return (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{accn.replace('-', '')}")


# ----------------------------- pure selection -----------------------------


def duration_days(f: Fact) -> int | None:
    return (f.end - f.start).days if f.start else None


def is_annual(f: Fact) -> bool:
    d = duration_days(f)
    return d is not None and ANNUAL_DAYS[0] <= d <= ANNUAL_DAYS[1]


def is_quarterly(f: Fact) -> bool:
    d = duration_days(f)
    return d is not None and QUARTER_DAYS[0] <= d <= QUARTER_DAYS[1]


def fy_label(f: Fact) -> int:
    """Fiscal year = calendar year of the period end (issuer convention)."""

    return f.end.year


def pick_annual(facts: list[Fact], fy: int | None) -> Fact | None:
    """Latest annual point, or the annual point for a specific fiscal year."""

    annuals = [f for f in facts if is_annual(f)]
    if fy is not None:
        annuals = [f for f in annuals if fy_label(f) == fy]
    return max(annuals, key=lambda f: f.end, default=None)


def pick_instant(facts: list[Fact], anchor_end: date | None,
                 fy: int | None) -> Fact | None:
    """Instant value at the fiscal-year-end anchor date (±14d), else latest."""

    instants = [f for f in facts if f.start is None]
    if anchor_end is not None:
        near = [f for f in instants if abs((f.end - anchor_end).days) <= 14]
        if near:
            return min(near, key=lambda f: abs((f.end - anchor_end).days))
    if fy is not None:
        instants = [f for f in instants if f.end.year == fy]
    return max(instants, key=lambda f: f.end, default=None)


def quarters_within(facts: list[Fact], period_start: date,
                    period_end: date) -> list[Fact]:
    """Distinct quarterly points falling inside an annual period."""

    seen: set[tuple] = set()
    out: list[Fact] = []
    for f in sorted(facts, key=lambda f: f.end):
        if is_quarterly(f) and f.start and f.start >= period_start \
                and f.end <= period_end and (f.start, f.end) not in seen:
            seen.add((f.start, f.end))
            out.append(f)
    return out


# ----------------------------- data access --------------------------------


def _cik(ticker: str) -> int:
    return int(resolve_ticker(ticker)["cik"])


def _fetch(cik: int, taxonomy: str, tag: str, unit: str,
           settings: Settings) -> list[Fact]:
    with connect(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT start_date, end_date, val, accn, form, filed
            FROM facts_dedup
            WHERE cik = %s AND taxonomy = %s AND tag = %s AND unit = %s
            """,
            (cik, taxonomy, tag, unit),
        )
        return [Fact(r[0], r[1], float(r[2]), r[3], r[4] or "", r[5])
                for r in cur.fetchall()]


def _fy_period(cik: int, fy: int, settings: Settings) -> tuple[date, date] | None:
    """The company's actual fiscal period for a year, from anchor metrics."""

    for anchor in ("revenue", "net_income", "operating_cash_flow"):
        for taxonomy, tag in METRICS[anchor]["tags"]:
            facts = _fetch(cik, taxonomy, tag, METRICS[anchor]["unit"], settings)
            point = pick_annual(facts, fy)
            if point and point.start:
                return point.start, point.end
    return None


# ----------------------------- public API ----------------------------------


def get_metric(ticker: str, metric: str, fy: int | None = None,
               settings: Settings | None = None) -> MetricPoint | None:
    """One verified number: latest annual value, or a specific fiscal year."""

    settings = settings or get_settings()
    spec = METRICS.get(metric)
    if spec is None:
        raise MetricError(
            f"Unknown metric {metric!r}. Available: {sorted(METRICS)}")
    ticker = ticker.strip().upper()
    cik = _cik(ticker)

    anchor_end: date | None = None
    if spec["kind"] == "instant":
        period = _fy_period(cik, fy, settings) if fy else None
        anchor_end = period[1] if period else None

    for taxonomy, tag in spec["tags"]:  # §5.1 priority fallback, per request
        facts = _fetch(cik, taxonomy, tag, spec["unit"], settings)
        point = (pick_annual(facts, fy) if spec["kind"] == "duration"
                 else pick_instant(facts, anchor_end, fy))
        if point is not None:
            return MetricPoint(
                ticker=ticker, metric=metric, fy=fy_label(point),
                value=point.val, unit=spec["unit"],
                period_start=point.start, period_end=point.end,
                accn=point.accn, form=point.form,
                source_url=filing_url(cik, point.accn),
            )
    return None


def get_series(ticker: str, metric: str, years: int = 5,
               settings: Settings | None = None) -> list[MetricPoint]:
    """Last N fiscal years, stitched across tag migrations (§5.1)."""

    settings = settings or get_settings()
    latest = get_metric(ticker, metric, settings=settings)
    if latest is None:
        return []
    out = [latest]
    for fy in range(latest.fy - 1, latest.fy - years, -1):
        p = get_metric(ticker, metric, fy=fy, settings=settings)
        if p is not None:
            out.append(p)
    return out


def yoy_growth(ticker: str, metric: str, fy: int | None = None,
               settings: Settings | None = None
               ) -> tuple[float, MetricPoint, MetricPoint] | None:
    """(growth_fraction, current_point, prior_point) — computed in code."""

    settings = settings or get_settings()
    cur = get_metric(ticker, metric, fy=fy, settings=settings)
    if cur is None:
        return None
    prev = get_metric(ticker, metric, fy=cur.fy - 1, settings=settings)
    if prev is None or prev.value == 0:
        return None
    return (cur.value - prev.value) / abs(prev.value), cur, prev


def cagr(ticker: str, metric: str, years: int,
         settings: Settings | None = None
         ) -> tuple[float, MetricPoint, MetricPoint] | None:
    """N-year compound annual growth rate between two verified endpoints."""

    settings = settings or get_settings()
    end = get_metric(ticker, metric, settings=settings)
    if end is None:
        return None
    start = get_metric(ticker, metric, fy=end.fy - years, settings=settings)
    if start is None or start.value <= 0 or end.value <= 0:
        return None
    return (end.value / start.value) ** (1 / years) - 1, end, start


# Ratio metrics: name -> (numerator_metric, denominator_metric)
RATIOS: dict[str, tuple[str, str]] = {
    "gross_margin": ("gross_profit", "revenue"),
    "operating_margin": ("operating_income", "revenue"),
    "net_margin": ("net_income", "revenue"),
    "rnd_intensity": ("research_and_development", "revenue"),
}


def ratio(ticker: str, name: str, fy: int | None = None,
          settings: Settings | None = None
          ) -> tuple[float, MetricPoint, MetricPoint] | None:
    """(ratio, numerator_point, denominator_point) for a named ratio."""

    if name not in RATIOS:
        raise MetricError(f"Unknown ratio {name!r}. Available: {sorted(RATIOS)}")
    settings = settings or get_settings()
    num_m, den_m = RATIOS[name]
    den = get_metric(ticker, den_m, fy=fy, settings=settings)
    if den is None or den.value == 0:
        return None
    num = get_metric(ticker, num_m, fy=den.fy, settings=settings)
    if num is None:
        return None
    return num.value / den.value, num, den


def q4_single_quarter(ticker: str, metric: str, fy: int | None = None,
                      settings: Settings | None = None) -> MetricPoint | None:
    """Q4 = FY − Q1 − Q2 − Q3 (§5.6: Q4 is never reported directly)."""

    settings = settings or get_settings()
    spec = METRICS.get(metric)
    if spec is None or spec["kind"] != "duration":
        raise MetricError(f"Q4 derivation needs a duration metric, got {metric!r}")
    annual = get_metric(ticker, metric, fy=fy, settings=settings)
    if annual is None or annual.period_start is None:
        return None
    cik = _cik(ticker)
    for taxonomy, tag in spec["tags"]:
        facts = _fetch(cik, taxonomy, tag, spec["unit"], settings)
        qs = quarters_within(facts, annual.period_start, annual.period_end)
        if len(qs) == 3:
            q4 = annual.value - sum(q.val for q in qs)
            return MetricPoint(
                ticker=annual.ticker, metric=f"{metric}_q4", fy=annual.fy,
                value=q4, unit=annual.unit,
                period_start=qs[-1].end, period_end=annual.period_end,
                accn=annual.accn, form=annual.form,
                source_url=annual.source_url, derived=True,
                note="Q4 = FY − Q1 − Q2 − Q3 (computed; Q4 is not reported)",
            )
    return None


def compare(tickers: list[str], metric: str, fy: int | None = None,
            settings: Settings | None = None) -> list[MetricPoint]:
    """Same metric across companies (each company's own latest/target FY)."""

    settings = settings or get_settings()
    out = []
    for t in tickers:
        p = get_metric(t, metric, fy=fy, settings=settings)
        if p is not None:
            out.append(p)
    return sorted(out, key=lambda p: p.value, reverse=True)
