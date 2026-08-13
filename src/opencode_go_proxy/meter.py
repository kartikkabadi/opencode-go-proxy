"""Append-only usage meter for opencode-go-proxy traffic.

Mirrors codex-router's `usage-events.jsonl`: one JSON object per line, written
after every upstream turn (streaming and non-streaming). Events use the
reference schema - ISO 8601 `at`, camelCase token/duration fields, plus
`meteringVersion` and `provider` - with the proxy's own markers kept additive
(`streamAborted`, `emptyCompletion`, `retries`, `kind`,
`estimatedInputTokens`). The accounting is deliberately honest:

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

import datetime
import json
import math
import os
import threading
from typing import Any

Json = dict[str, Any]

DEFAULT_STATE_DIR = os.path.join(os.path.expanduser("~"), ".codex", "opencode-go-proxy")
STATE_DIR_ENV = "OPENCODE_GO_PROXY_STATE_DIR"

# Canonical identity fields, matching the codex-router event schema. The
# versioned meteringVersion string marks events as written by this proxy
# (version 1 of the proxy-side schema), distinct from router events.
METERING_VERSION = "opencode-go-proxy/1"
PROVIDER = "opencode-go"

# Zero-input-token estimation: some upstreams report prompt_tokens: 0 even
# when the prompt is large, which breaks Codex's compaction heuristic. The
# proxy substitutes ceil(prompt_bytes / 3.3), floor 1000, capped at the
# model's context window, in a separate estimatedInputTokens field, and
# disables itself permanently for a model once the upstream reports real
# non-zero input tokens. OPENCODE_GO_PROXY_ESTIMATE_ZERO_INPUT=0 turns the
# substitution off entirely.
ESTIMATE_ZERO_INPUT_ENV = "OPENCODE_GO_PROXY_ESTIMATE_ZERO_INPUT"
DEFAULT_ESTIMATE_CONTEXT_WINDOW = 272000

_lock = threading.Lock()

_models_with_real_tokens: set[str] = set()


def state_dir() -> str:
    return os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_DIR


def usage_events_path() -> str:
    return os.path.join(state_dir(), "usage-events.jsonl")


def estimate_zero_input_disabled() -> bool:
    """True when the estimation kill switch is set (env value "0")."""
    return os.environ.get(ESTIMATE_ZERO_INPUT_ENV, "1") == "0"


def estimate_input_tokens(
    model: str,
    prompt_bytes: int,
    usage: Any,
    *,
    context_window: int | None = None,
) -> int | None:
    """Estimate input tokens for a turn whose upstream reported exactly 0.

    Returns None when the kill switch is set, the model has already reported
    real non-zero input tokens, usage is missing, or the reported count is not
    exactly 0. Otherwise returns max(1000, ceil(prompt_bytes / 3.3)) capped at
    the model's context window (DEFAULT_ESTIMATE_CONTEXT_WINDOW when unknown).
    """
    if estimate_zero_input_disabled():
        return None
    if model in _models_with_real_tokens:
        return None
    if not isinstance(usage, dict):
        return None
    reported = usage.get("prompt_tokens", usage.get("input_tokens"))
    if not (isinstance(reported, int) and reported == 0):
        return None
    cap = context_window if isinstance(context_window, int) and context_window > 0 else DEFAULT_ESTIMATE_CONTEXT_WINDOW
    estimate = max(1000, math.ceil(max(0, prompt_bytes) / 3.3))
    return min(cap, estimate)


def note_real_input_tokens(model: str) -> None:
    """Latch a model as reporting real input tokens; estimation never applies again."""
    if model:
        _models_with_real_tokens.add(model)


def clear_estimate_latches() -> None:
    """Drop the per-model real-token latches (test isolation only)."""
    _models_with_real_tokens.clear()


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


def _iso_at(value: float | None) -> str:
    """ISO 8601 UTC instant (Z) for an epoch-seconds value, or now."""
    if value is None:
        instant = datetime.datetime.now(datetime.UTC)
    else:
        instant = datetime.datetime.fromtimestamp(value, tz=datetime.UTC)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def record_usage_event(
    *,
    model: str | None,
    status: int,
    duration_ms: int,
    input_tokens: Any = None,
    output_tokens: Any = None,
    total_tokens: Any = None,
    estimated_input_tokens: Any = None,
    stream_aborted: bool = False,
    empty_completion: bool = False,
    retries: int | None = None,
    at: float | None = None,
    kind: str = "turn",
    provider: str | None = None,
) -> None:
    """Append one usage event to usage-events.jsonl, creating the dir on demand.

    Every event carries the canonical camelCase schema (`at` as ISO 8601,
    `inputTokens` / `outputTokens` / `totalTokens` / `durationMs`, plus
    `meteringVersion` and `provider`) so the same file is readable by
    codex-router-style consumers. Token fields are optional and only written
    when they are clean non-negative counts; a truncated stream never reports
    final usage, so callers simply omit them. Proxy-specific markers
    (`streamAborted`, `emptyCompletion`, `retries`, `kind`) are additive and
    only written when set. `provider` overrides the module constant so native
    turns meter under their own provider and never count against the
    opencode-go quota.
    """
    record: Json = {
        "at": _iso_at(at),
        "model": _safe_text(model, "unknown"),
        "provider": provider or PROVIDER,
        "meteringVersion": METERING_VERSION,
        "status": int(status),
        "durationMs": int(duration_ms),
    }
    for key, value in (
        ("inputTokens", input_tokens),
        ("outputTokens", output_tokens),
        ("totalTokens", total_tokens),
    ):
        clean = _safe_token(value)
        if clean is not None:
            record[key] = clean
    estimated = _safe_token(estimated_input_tokens)
    if estimated is not None:
        record["estimatedInputTokens"] = estimated
    if stream_aborted:
        record["streamAborted"] = True
    if empty_completion:
        record["emptyCompletion"] = True
    if retries:
        record["retries"] = int(retries)
    if kind and kind != "turn":
        record["kind"] = kind
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
