"""Pytest fixtures shared across the test suite."""

from __future__ import annotations

import pytest

import app.generation as generation
import app.refinement as refinement
import app.smoke_eval as smoke_eval
import app.verification as verification
from tests.llm_fakes import FakeAnthropic


@pytest.fixture(autouse=True)
def _fake_anthropic(monkeypatch):
    """Every test runs against the fake Anthropic client — no real API calls."""

    FakeAnthropic.queue = []
    FakeAnthropic.calls = []
    monkeypatch.setattr(generation.anthropic, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(verification.anthropic, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(refinement.anthropic, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(smoke_eval.anthropic, "Anthropic", FakeAnthropic)
