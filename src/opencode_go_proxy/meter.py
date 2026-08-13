"""Append-only usage meter for opencode-go-proxy traffic.

Mirrors codex-router's `usage-events.jsonl`: one JSON object per line, written
after every upstream turn (streaming and non-streaming). The accounting is
deliberately honest:

- A stream that died after its HTTP 200 head was already committed records
  `status: 502` plus a `streamAborted` marker and omits token counts, so a
  turn the client saw start but never finish is never counted as a success.
- An upstream that answered 200 but produced no output records
  `emptyCompletion`.
- Retries are recorded only when there were any; a transparently absorbed
  upstream failure still records its real status.
- Metering must never break a live request: every I/O error is swallowed.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

Json = dict[str, Any]

DEFAULT_STATE_DIR = os.path.join(os.path.expanduser("~"), ".codex", "opencode-go-proxy")
STATE_DIR_ENV = "OPENCODE_GO_PROXY_STATE_DIR"

_lock = threading.Lock()


def state_dir() -> str:
    return os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_DIR


def usage_events_path() -> str:
    return os.path.join(state_dir(), "usage-events.jsonl")


def _safe_token(value: Any) -> int | None:
    """Return a non-negative int, or None when the value isn't a clean count."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _safe_text(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def record_usage_event(
    *,
    model: str | None,
    status: int,
    duration_ms: int,
    input_tokens: Any = None,
    output_tokens: Any = None,
    total_tokens: Any = None,
    stream_aborted: bool = False,
    empty_completion: bool = False,
    retries: int | None = None,
    at: float | None = None,
) -> None:
    """Append one usage event to usage-events.jsonl, creating the dir on demand.

    Token fields are optional and only written when they are clean non-negative
    counts; a truncated stream never reports final usage, so callers simply
    omit them.
    """
    record: Json = {
        "model": _safe_text(model, "unknown"),
        "status": int(status),
        "duration_ms": int(duration_ms),
        "at": at if at is not None else time.time(),
    }
    for key, value in (
        ("input_tokens", input_tokens),
        ("output_tokens", output_tokens),
        ("total_tokens", total_tokens),
    ):
        clean = _safe_token(value)
        if clean is not None:
            record[key] = clean
    if stream_aborted:
        record["streamAborted"] = True
    if empty_completion:
        record["emptyCompletion"] = True
    if retries:
        record["retries"] = int(retries)
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    with _lock:
        try:
            path = usage_events_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        except OSError:
            # Metering is best-effort; never break a live request over it.
            pass
