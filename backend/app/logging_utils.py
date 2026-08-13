"""Structured jsonl logging (design-doc Section 6).

One JSON object per event, appended to a local ``.jsonl`` file. This is
deliberately minimal: no logging infrastructure, just a ``log_event`` helper
the pipeline wraps around each key step. Loading the file into pandas and
grouping by ``verdict`` is enough to chart the unsupported rate.

Example events::

    {"event": "query_received", "query_id": "q123", "ticker": "AAPL", ...}
    {"event": "retrieval", "query_id": "q123", "retrieved_chunk_ids": [...]}
    {"event": "claim_judged", "query_id": "q123", "claim_id": "c1", "verdict": "SUPPORTED"}
    {"event": "report_summary", "query_id": "q123", "unsupported_rate": 0.1}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def log_event(event: str, log_path: str | Path, **fields: Any) -> dict[str, Any]:
    """Append one structured event as a JSON line to ``log_path``.

    A UTC ``ts`` and the ``event`` name are always included. Extra keyword
    fields are merged in. Returns the record (handy for tests). Never raises on
    non-serialisable values — they are coerced to ``str`` via ``default=str``.
    """

    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }

    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    return record
