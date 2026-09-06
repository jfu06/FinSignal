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
    tag: str = ""                  # exact XBRL concept, e.g. us-gaap:GrossProfit
    stmt_url: str = ""             # rendered statement page holding the figure

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker, "metric": self.metric, "fy": self.fy,
            "value": self.value, "unit": self.unit,
            "period_start": str(self.period_start) if self.period_start else None,
            "period_end": str(self.period_end),
            "accn": self.accn, "form": self.form, "source_url": self.source_url,
            "derived": self.derived, "note": self.note,
        }


def filing_url(cik: int, accn: str, primary_doc: str | None = None) -> str:
    """The most verifiable SEC link we can build for a filing.

    With the primary document known: the iXBRL viewer on the 10-K text
    itself — every reported figure is a clickable tagged fact, so an
    analyst can search the shown XBRL tag and land on the exact number.
    Fallback: the human-readable filing index page (form type, date,
    document list) — never the bare archive directory.
    """

    if primary_doc:
        return (f"https://www.sec.gov/ix?doc=/Archives/edgar/data/{cik}/"
                f"{accn.replace('-', '')}/{primary_doc}")
    return (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{accn.replace('-', '')}/{accn}-index.htm")


# Which rendered financial statement each metric lives on. Drives the
# statement-page provenance link (the R#.htm income statement / balance
# sheet / cash flow rendering EDGAR generates for every filing).
_STMT_BY_METRIC: dict[str, str] = {
    "revenue": "income", "cost_of_revenue": "income", "gross_profit": "income",
    "research_and_development": "income", "sga_expense": "income",
    "operating_income": "income", "interest_expense": "income",
    "pretax_income": "income",
    "net_income": "income", "eps_basic": "income", "eps_diluted": "income",
    "total_assets": "balance", "total_liabilities": "balance",
    "stockholders_equity": "balance", "cash_and_equivalents": "balance",
    "current_assets": "balance", "current_liabilities": "balance",
    "inventory": "balance", "accounts_payable": "balance",
    "accounts_receivable": "balance", "ppe_net": "balance",
    "long_term_debt": "balance",
    "operating_cash_flow": "cashflow", "share_buybacks": "cashflow",
    "capex": "cashflow", "depreciation_amortization": "cashflow",
    "dividends_paid": "cashflow",
    # shares_outstanding lives on the cover page — no statement link
}


def classify_statements(xml_text: str) -> dict[str, str]:
    """FilingSummary.xml -> {"income"|"balance"|"cashflow": "R#.htm"}.

    First matching report per statement (lowest position), skipping
    parenthetical and comprehensive-income variants.
    """

    import xml.etree.ElementTree as ET

    out: dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for report in root.iter("Report"):
        name = (report.findtext("ShortName") or "").upper()
        html = report.findtext("HtmlFileName") or ""
        if not html or "PARENTHETICAL" in name:
            continue
        if ("balance" not in out
                and ("BALANCE SHEET" in name or "FINANCIAL POSITION" in name)):
            out["balance"] = html
        elif "cashflow" not in out and "CASH FLOW" in name:
            out["cashflow"] = html
        elif ("income" not in out and "COMPREHENSIVE" not in name
                and ("STATEMENTS OF OPERATIONS" in name
                     or "STATEMENT OF OPERATIONS" in name
                     or "OF INCOME" in name or "OF EARNINGS" in name
                     or "INCOME STATEMENT" in name)):
            out["income"] = html
    return out


_STMT_CACHE: dict[tuple[int, str], dict[str, str]] = {}


def _stmt_url(cik: int, accn: str, metric: str,
              settings: Settings) -> str:
    """Link to the rendered statement the metric's figure sits on ("" if n/a).

    DB-cached; on first sight of a filing, one FilingSummary.xml fetch.
    Provenance must never break an answer: any failure returns "".
    """

    stmt = _STMT_BY_METRIC.get(metric)
    if stmt is None:
        return ""
    key = (cik, accn)
    pages = _STMT_CACHE.get(key)
    try:
        if pages is None:
            with connect(settings) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT stmt, html_file FROM filing_stmt_pages "
                    "WHERE cik = %s AND accn = %s", (cik, accn))
                pages = dict(cur.fetchall())
            if not pages:
                from .edgar import _get
                xml_text = _get(
                    f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                    f"{accn.replace('-', '')}/FilingSummary.xml")
                pages = classify_statements(xml_text)
                if pages:
                    with connect(settings) as conn, conn.cursor() as cur:
                        cur.executemany(
                            """
                            INSERT INTO filing_stmt_pages
                                (cik, accn, stmt, html_file)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (cik, accn, stmt) DO NOTHING
                            """,
                            [(cik, accn, s, f) for s, f in pages.items()])
                        conn.commit()
            _STMT_CACHE[key] = pages
    except Exception:  # noqa: BLE001
        _STMT_CACHE[key] = pages = pages or {}
    html = pages.get(stmt)
    if not html:
        return ""
    return (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{accn.replace('-', '')}/{html}")


_DOC_CACHE: dict[int, dict[str, str]] = {}


def _primary_doc(cik: int, accn: str, settings: Settings) -> str | None:
    docs = _DOC_CACHE.get(cik)
    if docs is None:
        try:
            with connect(settings) as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT accn, primary_doc FROM filing_docs WHERE cik = %s",
                    (cik,))
                docs = dict(cur.fetchall())
        except Exception:  # noqa: BLE001 — provenance must never break answers
            docs = {}
        _DOC_CACHE[cik] = docs
    return docs.get(accn)


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


# The metrics layer trusts ANNUAL REPORTS. Schwab's DEF 14A proxy tagged
# NetIncomeLoss for FY2025 as 8,852,000 (a 1000x mis-scale of the 10-K's
# 8,852,000,000), filed two months later — and dedup-by-latest-filed let a
# proxy statement outvote the 10-K, printing a 0.0% net margin. Proxies,
# 8-Ks and prospectuses never get a vote here.
_ANNUAL_FORMS = ("10-K", "10-K/A")
_REPORT_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A")  # Q4 derivation only


# Short-lived facts cache: a 3-year margin trend used to issue 6+
# identical Neon round trips (per year x per tag); facts only change at
# ingest, so a 10-minute TTL is safe and cuts trend queries to 2 fetches.
_FACTS_CACHE: dict[tuple, tuple[float, list]] = {}
_FACTS_TTL_S = 600.0


def _fetch(cik: int, taxonomy: str, tag: str, unit: str,
           settings: Settings, accn: str | None = None,
           forms: tuple = _ANNUAL_FORMS) -> list[Fact]:
    import time as _time

    cache_key = (cik, taxonomy, tag, unit, accn, forms)
    hit = _FACTS_CACHE.get(cache_key)
    if hit is not None and _time.monotonic() - hit[0] < _FACTS_TTL_S:
        return hit[1]
    with connect(settings) as conn, conn.cursor() as cur:
        if accn is not None:
            # Oracle-document mode: only figures AS PRINTED in that one filing
            # (incl. its comparative prior periods).
            cur.execute(
                """
                SELECT DISTINCT ON (start_date, end_date)
                    start_date, end_date, val, accn, form, filed
                FROM xbrl_facts
                WHERE cik = %s AND taxonomy = %s AND tag = %s AND unit = %s
                  AND accn = %s
                ORDER BY start_date, end_date
                """,
                (cik, taxonomy, tag, unit, accn),
            )
        else:
            # Dedup AFTER the form filter — filtering the facts_dedup view
            # would drop periods whose latest-filed row is a non-report.
            cur.execute(
                """
                SELECT DISTINCT ON (start_date, end_date)
                    start_date, end_date, val, accn, form, filed
                FROM xbrl_facts
                WHERE cik = %s AND taxonomy = %s AND tag = %s AND unit = %s
                  AND form = ANY(%s)
                ORDER BY start_date, end_date, filed DESC, accn DESC
                """,
                (cik, taxonomy, tag, unit, list(forms)),
            )
        facts = [Fact(r[0], r[1], float(r[2]), r[3], r[4] or "", r[5])
                 for r in cur.fetchall()]
    if len(_FACTS_CACHE) > 4096:
        _FACTS_CACHE.clear()
    _FACTS_CACHE[cache_key] = (_time.monotonic(), facts)
    return facts


def _fy_period(cik: int, fy: int, settings: Settings,
               accn: str | None = None) -> tuple[date, date] | None:
    """The company's actual fiscal period for a year, from anchor metrics."""

    for anchor in ("revenue", "net_income", "operating_cash_flow"):
        for taxonomy, tag in METRICS[anchor]["tags"]:
            facts = _fetch(cik, taxonomy, tag, METRICS[anchor]["unit"],
                           settings, accn=accn)
            point = pick_annual(facts, fy)
            if point and point.start:
                return point.start, point.end
    return None


# ----------------------------- public API ----------------------------------


def get_metric(ticker: str, metric: str, fy: int | None = None,
               settings: Settings | None = None,
               cik: int | None = None,
               accn: str | None = None) -> MetricPoint | None:
    """One verified number: latest annual value, or a specific fiscal year.

    ``cik`` bypasses ticker resolution (benchmark companies use display
    names, some delisted); ``accn`` restricts to figures as printed in one
    specific filing (oracle-document mode).
    """

    settings = settings or get_settings()
    spec = METRICS.get(metric)
    if spec is None:
        raise MetricError(
            f"Unknown metric {metric!r}. Available: {sorted(METRICS)}")
    ticker = ticker.strip().upper()
    if cik is None:
        cik = _cik(ticker)

    anchor_end: date | None = None
    if spec["kind"] == "instant":
        period = _fy_period(cik, fy, settings, accn=accn) if fy else None
        anchor_end = period[1] if period else None

    for taxonomy, tag in spec["tags"]:  # §5.1 priority fallback, per request
        facts = _fetch(cik, taxonomy, tag, spec["unit"], settings, accn=accn)
        point = (pick_annual(facts, fy) if spec["kind"] == "duration"
                 else pick_instant(facts, anchor_end, fy))
        if point is not None:
            return MetricPoint(
                ticker=ticker, metric=metric, fy=fy_label(point),
                value=point.val, unit=spec["unit"],
                period_start=point.start, period_end=point.end,
                accn=point.accn, form=point.form,
                source_url=filing_url(
                    cik, point.accn, _primary_doc(cik, point.accn, settings)),
                tag=f"{taxonomy}:{tag}",
                stmt_url=_stmt_url(cik, point.accn, metric, settings),
            )
    return None


def get_series(ticker: str, metric: str, years: int = 5,
               settings: Settings | None = None,
               cik: int | None = None,
               accn: str | None = None) -> list[MetricPoint]:
    """Last N fiscal years, stitched across tag migrations (§5.1)."""

    settings = settings or get_settings()
    latest = get_metric(ticker, metric, settings=settings, cik=cik, accn=accn)
    if latest is None:
        return []
    out = [latest]
    for fy in range(latest.fy - 1, latest.fy - years, -1):
        p = get_metric(ticker, metric, fy=fy, settings=settings,
                       cik=cik, accn=accn)
        if p is not None:
            out.append(p)
    return out


def yoy_growth(ticker: str, metric: str, fy: int | None = None,
               settings: Settings | None = None,
               cik: int | None = None,
               accn: str | None = None
               ) -> tuple[float, MetricPoint, MetricPoint] | None:
    """(growth_fraction, current_point, prior_point) — computed in code."""

    settings = settings or get_settings()
    cur = get_metric(ticker, metric, fy=fy, settings=settings,
                     cik=cik, accn=accn)
    if cur is None:
        return None
    prev = get_metric(ticker, metric, fy=cur.fy - 1, settings=settings,
                      cik=cik, accn=accn)
    if prev is None or prev.value == 0:
        return None
    return (cur.value - prev.value) / abs(prev.value), cur, prev


def cagr(ticker: str, metric: str, years: int,
         settings: Settings | None = None,
         cik: int | None = None,
         accn: str | None = None
         ) -> tuple[float, MetricPoint, MetricPoint] | None:
    """N-year compound annual growth rate between two verified endpoints."""

    settings = settings or get_settings()
    end = get_metric(ticker, metric, settings=settings, cik=cik, accn=accn)
    if end is None:
        return None
    start = get_metric(ticker, metric, fy=end.fy - years, settings=settings,
                       cik=cik, accn=accn)
    if start is None or start.value <= 0 or end.value <= 0:
        return None
    return (end.value / start.value) ** (1 / years) - 1, end, start


# Ratio metrics: name -> (numerator_metric, denominator_metric)
RATIOS: dict[str, tuple[str, str]] = {
    "gross_margin": ("gross_profit", "revenue"),
    "operating_margin": ("operating_income", "revenue"),
    "net_margin": ("net_income", "revenue"),
    "rnd_intensity": ("research_and_development", "revenue"),
    "pretax_margin": ("pretax_income", "revenue"),
    "capex_intensity": ("capex", "revenue"),
    "da_margin": ("depreciation_amortization", "revenue"),
}


def ratio(ticker: str, name: str, fy: int | None = None,
          settings: Settings | None = None,
          cik: int | None = None,
          accn: str | None = None
          ) -> tuple[float, MetricPoint, MetricPoint] | None:
    """(ratio, numerator_point, denominator_point) for a named ratio."""

    if name not in RATIOS:
        raise MetricError(f"Unknown ratio {name!r}. Available: {sorted(RATIOS)}")
    settings = settings or get_settings()
    num_m, den_m = RATIOS[name]
    den = get_metric(ticker, den_m, fy=fy, settings=settings,
                     cik=cik, accn=accn)
    if den is None or den.value == 0:
        return None
    num = get_metric(ticker, num_m, fy=den.fy, settings=settings,
                     cik=cik, accn=accn)
    if num is None:
        return None
    return num.value / den.value, num, den


def q4_single_quarter(ticker: str, metric: str, fy: int | None = None,
                      settings: Settings | None = None,
                      cik: int | None = None,
                      accn: str | None = None) -> MetricPoint | None:
    """Q4 = FY − Q1 − Q2 − Q3 (§5.6: Q4 is never reported directly)."""

    settings = settings or get_settings()
    spec = METRICS.get(metric)
    if spec is None or spec["kind"] != "duration":
        raise MetricError(f"Q4 derivation needs a duration metric, got {metric!r}")
    annual = get_metric(ticker, metric, fy=fy, settings=settings,
                        cik=cik, accn=accn)
    if annual is None or annual.period_start is None:
        return None
    if cik is None:
        cik = _cik(ticker)
    for taxonomy, tag in spec["tags"]:
        facts = _fetch(cik, taxonomy, tag, spec["unit"], settings, accn=accn,
                       forms=_REPORT_FORMS)
        qs = quarters_within(facts, annual.period_start, annual.period_end)
        if len(qs) == 3:
            q4 = annual.value - sum(q.val for q in qs)
            return MetricPoint(
                ticker=annual.ticker, metric=f"{metric}_q4", fy=annual.fy,
                value=q4, unit=annual.unit,
                period_start=qs[-1].end, period_end=annual.period_end,
                accn=annual.accn, form=annual.form,
                source_url=annual.source_url, derived=True, tag=annual.tag,
                stmt_url=annual.stmt_url,
                note="Q4 = FY − Q1 − Q2 − Q3 (computed; Q4 is not reported)",
            )
    return None


# Derived metrics: multi-component formulas over registry metrics, computed
# in code at a single consistent fiscal year (FinanceBench failure analysis:
# quick ratio / working capital questions need subtraction, not just num/den).
DERIVED: dict[str, dict] = {
    "working_capital": {
        "components": ("current_assets", "current_liabilities"),
        "formula": lambda a, li: a - li, "unit": "USD",
        "note": "current assets − current liabilities",
    },
    "current_ratio": {
        "components": ("current_assets", "current_liabilities"),
        "formula": lambda a, li: a / li, "unit": "x",
        "note": "current assets / current liabilities",
    },
    "quick_ratio": {
        "components": ("current_assets", "inventory", "current_liabilities"),
        "formula": lambda a, inv, li: (a - inv) / li, "unit": "x",
        "note": "(current assets − inventory) / current liabilities",
    },
    # FinanceBench's DPO definition (purchases-adjusted denominator)
    "days_payable_outstanding": {
        "components": ("accounts_payable", "accounts_payable@prev",
                       "cost_of_revenue", "inventory", "inventory@prev"),
        "formula": lambda ap, ap0, cogs, inv, inv0:
            365 * ((ap + ap0) / 2) / (cogs + inv - inv0),
        "unit": "days",
        "note": "365 × avg(accounts payable) / (COGS + Δinventory)",
    },
}


def derived_metric(ticker: str, name: str, fy: int | None = None,
                   settings: Settings | None = None,
                   cik: int | None = None,
                   accn: str | None = None
                   ) -> tuple[float, list[MetricPoint]] | None:
    """(value, component_points) for a DERIVED formula, all at the same FY."""

    spec = DERIVED.get(name)
    if spec is None:
        raise MetricError(
            f"Unknown derived metric {name!r}. Available: {sorted(DERIVED)}")
    settings = settings or get_settings()
    points: list[MetricPoint] = []
    for i, component in enumerate(spec["components"]):
        name_part, _, suffix = component.partition("@")
        base_fy = fy if i == 0 else points[0].fy
        want_fy = (base_fy - 1) if suffix == "prev" and base_fy else base_fy
        p = get_metric(ticker, name_part, fy=want_fy,
                       settings=settings, cik=cik, accn=accn)
        if p is None:
            return None
        points.append(p)
    try:
        value = spec["formula"](*(p.value for p in points))
    except ZeroDivisionError:
        return None
    return value, points


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
