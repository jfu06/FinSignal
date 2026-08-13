"""Central configuration for FinSignal Phase 1.

All secrets come from the environment / a local ``.env`` file — never from
source. ``DATABASE_URL`` is read straight from the environment; the LLM and
embedding keys are loaded from ``.env`` via python-dotenv. Nothing here
connects to anything at import time, so the module is safe to import in tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# backend/ is the project root for the runtime; load backend/.env if present.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_BACKEND_ROOT / ".env")


class ConfigError(RuntimeError):
    """Raised when a required configuration value is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Resolved runtime configuration."""

    database_url: str
    anthropic_api_key: str
    llm_model: str
    assess_model: str
    embedding_model: str
    embedding_dim: int
    top_k: int
    unsupported_rate_threshold: float
    log_path: Path
    # --- public-deployment guardrails (all no-ops by default for local dev) ---
    access_code: str            # non-empty -> UI requires this code to enter
    session_query_limit: int    # max questions per browser session
    daily_query_budget: int     # max questions per UTC day across all users
    max_tickers: int            # cap on corpus size via on-demand onboarding


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise ConfigError(
            f"Missing required environment variable {name!r}. "
            f"Copy backend/.env.example to backend/.env and fill it in."
        )
    return value.strip()


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"{name} must be a float, got {raw!r}") from exc


def get_settings() -> Settings:
    """Build a :class:`Settings` from the environment.

    Raises :class:`ConfigError` with an actionable message when a required
    value is missing, so failures are safe and clear rather than a bare
    ``KeyError`` deep in the pipeline.
    """

    log_path = Path(os.getenv("LOG_PATH", "logs/events.jsonl"))
    if not log_path.is_absolute():
        log_path = _BACKEND_ROOT / log_path

    return Settings(
        database_url=_require("DATABASE_URL"),
        anthropic_api_key=_require("ANTHROPIC_API_KEY"),
        llm_model=os.getenv("LLM_MODEL", "claude-sonnet-5"),
        # Small/fast model for low-stakes decisions (retrieval sufficiency,
        # smoke-eval question drafting). Failures fail safe, so speed wins.
        assess_model=os.getenv("ASSESS_MODEL", "claude-haiku-4-5-20251001"),
        embedding_model=os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-small"),
        embedding_dim=_int("EMBEDDING_DIM", 384),
        top_k=_int("TOP_K", 6),
        unsupported_rate_threshold=_float("UNSUPPORTED_RATE_THRESHOLD", 0.04),
        log_path=log_path,
        access_code=(os.getenv("ACCESS_CODE") or "").strip(),
        session_query_limit=_int("SESSION_QUERY_LIMIT", 10),
        daily_query_budget=_int("DAILY_QUERY_BUDGET", 50),
        max_tickers=_int("MAX_TICKERS", 10),
    )
