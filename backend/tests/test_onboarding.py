"""Unit tests for on-demand ticker onboarding (network mocked)."""

from __future__ import annotations

from pathlib import Path

import pytest

import app.edgar as edgar
import app.onboarding as onboarding
from app.edgar import EdgarError, resolve_ticker
from tests.llm_fakes import make_settings

FAKE_MAP = {
    "AAPL": {"cik": "0000320193", "title": "Apple Inc."},
    "NVDA": {"cik": "0001045810", "title": "NVIDIA Corp"},
}


@pytest.fixture(autouse=True)
def _offline_map(monkeypatch):
    monkeypatch.setattr(edgar, "load_ticker_map", lambda force_refresh=False: FAKE_MAP)


class TestResolveTicker:
    def test_known_ticker_resolves(self):
        assert resolve_ticker("nvda")["cik"] == "0001045810"

    def test_unknown_ticker_raises_friendly_error(self):
        with pytest.raises(EdgarError, match="not found in the SEC"):
            resolve_ticker("ZZZZ")

    @pytest.mark.parametrize("bad", ["", "123ABC", "AA PL", "way-too-long-ticker"])
    def test_invalid_format_rejected_before_lookup(self, bad):
        with pytest.raises(EdgarError, match="not a valid ticker"):
            resolve_ticker(bad)


class TestEnsureTicker:
    def test_existing_ticker_short_circuits(self, monkeypatch, tmp_path):
        monkeypatch.setattr(onboarding, "known_tickers", lambda s: {"AAPL"})
        called = []
        monkeypatch.setattr(onboarding, "resolve_ticker",
                            lambda t: called.append(t))
        result = onboarding.ensure_ticker("aapl", settings=make_settings(tmp_path))
        assert result["status"] == "exists"
        assert called == []  # no network path touched

    def test_new_ticker_downloads_and_ingests(self, monkeypatch, tmp_path):
        steps: list[str] = []
        monkeypatch.setattr(onboarding, "known_tickers", lambda s: {"AAPL"})
        monkeypatch.setattr(onboarding, "resolve_ticker",
                            lambda t: FAKE_MAP["NVDA"])
        monkeypatch.setattr(onboarding, "latest_10k",
                            lambda cik: {"filing_date": "2026-02-26"})
        monkeypatch.setattr(onboarding, "download_10k",
                            lambda t, cik: Path("/fake/NVDA_10K.txt"))
        monkeypatch.setattr(onboarding, "ingest_file",
                            lambda path, settings: 300)
        import app.xbrl as xbrl
        monkeypatch.setattr(xbrl, "ingest_facts",
                            lambda t, settings: {"facts": 27281})
        result = onboarding.ensure_ticker(
            "NVDA", settings=make_settings(tmp_path), progress=steps.append)
        assert result == {
            "status": "added", "ticker": "NVDA", "company": "NVIDIA Corp",
            "filing_date": "2026-02-26", "chunks": 300, "facts": 27281,
        }
        assert len(steps) == 5  # resolve, locate, download, ingest, xbrl

    def test_xbrl_failure_does_not_block_onboarding(self, monkeypatch, tmp_path):
        monkeypatch.setattr(onboarding, "known_tickers", lambda s: set())
        monkeypatch.setattr(onboarding, "resolve_ticker",
                            lambda t: FAKE_MAP["NVDA"])
        monkeypatch.setattr(onboarding, "latest_10k",
                            lambda cik: {"filing_date": "2026-02-26"})
        monkeypatch.setattr(onboarding, "download_10k",
                            lambda t, cik: __import__("pathlib").Path("/f.txt"))
        monkeypatch.setattr(onboarding, "ingest_file",
                            lambda path, settings: 300)
        import app.xbrl as xbrl

        def boom(t, settings):
            raise RuntimeError("SEC hiccup")

        monkeypatch.setattr(xbrl, "ingest_facts", boom)
        result = onboarding.ensure_ticker(
            "NVDA", settings=make_settings(tmp_path))
        assert result["status"] == "added"   # document line still succeeded
        assert result["facts"] == 0

    def test_unknown_ticker_error_propagates(self, monkeypatch, tmp_path):
        monkeypatch.setattr(onboarding, "known_tickers", lambda s: set())
        with pytest.raises(EdgarError):
            onboarding.ensure_ticker("ZZZZ", settings=make_settings(tmp_path))
