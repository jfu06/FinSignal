"""Anthropic client factory — single creation point, optional LangSmith tracing.

Every pipeline stage gets its client here. When ``LANGSMITH_TRACING=true``
(and ``LANGSMITH_API_KEY`` is set), the client is wrapped with LangSmith's
Anthropic wrapper so each LLM call appears in the LangSmith trace tree next to
the LangGraph node spans. With tracing off — the default — this returns a
plain client: zero behavior change, zero new dependencies on the hot path.

Fail-open by design: a missing/broken langsmith install or a bad key must
never take the pipeline down, so any wrapping error falls back to the plain
client. The jsonl event log remains the source of truth for cost accounting
and debugging; LangSmith is an additional viewing layer.
"""

from __future__ import annotations

import os

import anthropic

from .config import Settings


def _tracing_enabled() -> bool:
    return os.getenv("LANGSMITH_TRACING", "").strip().lower() == "true"


def anthropic_client(settings: Settings, max_retries: int = 4) -> anthropic.Anthropic:
    """Anthropic client with retries, LangSmith-wrapped when tracing is on."""

    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key, max_retries=max_retries
    )
    if not _tracing_enabled():
        return client
    try:
        from langsmith.wrappers import wrap_anthropic

        return wrap_anthropic(client)
    except Exception:  # noqa: BLE001 — tracing must never break the pipeline
        return client


def openai_client(settings: Settings, max_retries: int = 4):
    """OpenAI client (cross-vendor judge), LangSmith-wrapped when tracing is on.

    Imported lazily so the default anthropic-only configuration never touches
    the openai package at runtime.
    """

    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key, max_retries=max_retries)
    if not _tracing_enabled():
        return client
    try:
        from langsmith.wrappers import wrap_openai

        return wrap_openai(client)
    except Exception:  # noqa: BLE001 — tracing must never break the pipeline
        return client
