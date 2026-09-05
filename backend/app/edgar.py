"""SEC EDGAR client: ticker resolution + 10-K download (official, free, public).

Supports ANY US-listed ticker via SEC's company_tickers.json mapping —
no hardcoded CIK list. SEC asks only for a descriptive User-Agent with a
contact address and <= 10 req/s (we make ~3 per onboarding).
"""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

import certifi

DATA_RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
USER_AGENT = "FinSignal research demo jiayi_fu@alumni.upenn.edu"
_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_TICKER_MAP_CACHE = DATA_RAW / "company_tickers.json"
_TICKER_MAP_MAX_AGE_S = 7 * 24 * 3600  # refresh weekly

# macOS python.org builds don't wire up system CAs; use certifi's bundle.
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

_BLOCK_TAGS = {
    "p", "div", "tr", "table", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
}


class EdgarError(RuntimeError):
    """A friendly, user-displayable EDGAR failure (unknown ticker, no 10-K…)."""


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
        return resp.read()


class _TextExtractor(HTMLParser):
    """Strip tags from (inline-XBRL) filing HTML, keeping block boundaries."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        raw = raw.replace("\xa0", " ")
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r" ?\n ?", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


def load_ticker_map(force_refresh: bool = False) -> dict[str, dict]:
    """Return {TICKER: {cik, title}} from SEC's official mapping (cached weekly)."""

    stale = (
        force_refresh
        or not _TICKER_MAP_CACHE.exists()
        or time.time() - _TICKER_MAP_CACHE.stat().st_mtime > _TICKER_MAP_MAX_AGE_S
    )
    if stale:
        DATA_RAW.mkdir(parents=True, exist_ok=True)
        _TICKER_MAP_CACHE.write_bytes(_get(_TICKER_MAP_URL))
    raw = json.loads(_TICKER_MAP_CACHE.read_text(encoding="utf-8"))
    return {
        entry["ticker"].upper(): {
            "cik": f"{int(entry['cik_str']):010d}",
            "title": entry["title"],
        }
        for entry in raw.values()
    }


def resolve_ticker(ticker: str) -> dict:
    """Return {cik, title} for a ticker, or raise EdgarError."""

    ticker = ticker.strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", ticker):
        raise EdgarError(f"{ticker!r} is not a valid ticker symbol format")
    mapping = load_ticker_map()
    entry = mapping.get(ticker)
    if entry is None:
        raise EdgarError(
            f"Ticker {ticker!r} not found in the SEC company registry — please confirm it is a US-listed symbol"
        )
    return entry


def latest_10k(cik: str) -> dict:
    """Return {accession, primary_doc, filing_date, form} for the newest 10-K."""

    subs = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    recent = subs["filings"]["recent"]
    for form, acc, doc, date in zip(
        recent["form"], recent["accessionNumber"],
        recent["primaryDocument"], recent["filingDate"],
    ):
        if form == "10-K":  # exact match: skip 10-K/A amendments
            return {"accession": acc, "primary_doc": doc,
                    "filing_date": date, "form": form}
    raise EdgarError(
        "No recent 10-K found for this company (foreign issuers usually file 20-F, which is not supported yet)"
    )


def filing_for_fy(cik: str, fy: int, form: str = "10-K") -> dict:
    """Locate the historical filing whose fiscal-period END falls in ``fy``.

    Used by the FinanceBench benchmark ingester (doc names carry the fiscal
    year, e.g. 3M_2018_10K → the 10-K with reportDate 2018-12-31). Matches on
    reportDate year, which equals our FY-label convention (calendar year of
    period end — Walmart FY2015 ends 2015-01-31 → reportDate year 2015).
    Searches the "recent" window first, then the older paginated submission
    files, since big filers push 2015-era filings past the 1000-row window.
    """

    def _rows(block: dict):
        yield from zip(
            block["form"], block["accessionNumber"], block["primaryDocument"],
            block["filingDate"], block["reportDate"],
        )

    def _scan(block: dict, match) -> dict | None:
        for f, acc, doc, filed, report in _rows(block):
            if f == form and match(report or ""):
                return {"accession": acc, "primary_doc": doc,
                        "filing_date": filed, "report_date": report, "form": f}
        return None

    exact = lambda r: r[:4] == str(fy)  # noqa: E731
    # Retailers with a Jan/Feb fiscal-year end label the FY by its start-side
    # year (Ulta "FY2023" ends 2024-02-03), so accept fy+1 Q1 as a fallback.
    spill = lambda r: (r[:4] == str(fy + 1)  # noqa: E731
                       and r[5:7] in ("01", "02", "03"))

    subs = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))
    blocks = [subs["filings"]["recent"]]
    hit = _scan(blocks[0], exact)
    # Page into older submission files only while the target year is missing —
    # big filers push 2015-era filings past the 1000-row "recent" window.
    if hit is None:
        for extra in subs["filings"].get("files", []):
            time.sleep(0.15)  # stay well under SEC's 10 req/s
            block = json.loads(
                _get(f"https://data.sec.gov/submissions/{extra['name']}"))
            blocks.append(block)
            hit = _scan(block, exact)
            if hit is not None:
                break
    if hit is None:  # fiscal-year label spills into fy+1 Q1 (retail FYE)
        for block in blocks:
            hit = _scan(block, spill)
            if hit is not None:
                break
    if hit is None:
        raise EdgarError(f"No {form} with report year {fy} found for CIK {cik}")
    return hit


def download_filing_text(cik: str, accession: str, primary_doc: str) -> str:
    """Fetch one filing's primary document and return it as plain text."""

    acc_nodash = accession.replace("-", "")
    url = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{acc_nodash}/{primary_doc}"
    )
    html = _get(url).decode("utf-8", errors="replace")
    text = html_to_text(html)
    if len(text) < 10_000:
        raise EdgarError(f"{accession}: extracted text unusually short "
                         f"({len(text)} chars) — not a full filing?")
    return text


def download_10k(ticker: str, cik: str | None = None) -> Path:
    """Download the latest 10-K as plain text into data/raw/. Returns the path."""

    ticker = ticker.strip().upper()
    if cik is None:
        cik = resolve_ticker(ticker)["cik"]
    meta = latest_10k(cik)
    acc_nodash = meta["accession"].replace("-", "")
    url = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{acc_nodash}/{meta['primary_doc']}"
    )
    html = _get(url).decode("utf-8", errors="replace")
    text = html_to_text(html)
    if len(text) < 10_000:
        raise EdgarError("Downloaded file is unusually short — it may not be a complete 10-K")

    DATA_RAW.mkdir(parents=True, exist_ok=True)
    out = DATA_RAW / f"{ticker}_10K.txt"
    out.write_text(text, encoding="utf-8")
    (DATA_RAW / f"{ticker}_10K.meta.json").write_text(
        json.dumps({"ticker": ticker, "source_url": url, **meta}, indent=2),
        encoding="utf-8",
    )
    return out
