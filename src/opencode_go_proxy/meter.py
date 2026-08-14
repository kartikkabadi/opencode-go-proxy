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
  `status: 502` plus an `emptyCompletion` marker: the client-visible
  outcome is a `response.error` (empty_completion), never a successful
  turn.
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
from dataclasses import dataclass, field
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

# In-process fold of usage-events.jsonl: the aggregate is updated on every
# append (under _lock) and served when the file identity is unchanged, so
# /state never rescans the whole meter file. The fold covers exactly the
# bytes [0, offset) of the file it was built from, keyed by a stat
# fingerprint so a shrink or an in-place rewrite forces a full rescan.
_FOLD_MARKER_BYTES = 256


@dataclass
class _Aggregate:
    """Folded counts for one provider bucket (key None = all providers)."""
    by_day_tokens: dict[str, int] = field(default_factory=dict)
    by_day_turns: dict[str, int] = field(default_factory=dict)
    last_model: str | None = None


@dataclass
class _Fold:
    """The folded state plus the exact file identity it was folded from."""
    fingerprint: tuple[int, int, int, int]  # (st_dev, st_ino, st_size, st_mtime_ns)
    offset: int  # byte offset (EOF at fold time) the fold covers
    bucketing_tz: datetime.tzinfo | None  # day-bucketing timezone the fold used
    marker: bytes  # last bytes of the file at fold time; verifies appends
    aggregates: dict[str | None, _Aggregate] = field(default_factory=dict)


_fold: _Fold | None = None


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


_DAY_KEYS = ("inputTokens", "outputTokens")


def _event_tokens(event: Json) -> int:
    """Tokens for one event: totalTokens, or the zero-token estimate when the
    upstream reported 0 (estimatedInputTokens plus any real output)."""
    total = _safe_token(event.get("totalTokens"))
    if total is not None and total > 0:
        return total
    estimated = _safe_token(event.get("estimatedInputTokens"))
    if estimated is not None and estimated > 0:
        output = _safe_token(event.get("outputTokens")) or 0
        return estimated + output
    if total is not None:
        return total
    return sum(_safe_token(event.get(key)) or 0 for key in _DAY_KEYS)


def _local_day(value: Any, now: datetime.datetime) -> str | None:
    """ISO day string (YYYY-MM-DD) in ``now``'s timezone for an event ``at``."""
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=datetime.UTC)
    return instant.astimezone(now.tzinfo).date().isoformat()


def _fingerprint(stat: os.stat_result) -> tuple[int, int, int, int]:
    """File identity the fold is valid for: inode, size, and mtime."""
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _same_bucketing_tz(a: datetime.tzinfo | None, b: datetime.tzinfo | None) -> bool:
    """Whether two tzinfo objects bucket events into the same local days."""
    if a is None or b is None:
        return a is None and b is None
    return a == b


def _tail_bytes(path: str) -> bytes:
    """Last ``_FOLD_MARKER_BYTES`` bytes of a file (the append-verification marker)."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        end = handle.tell()
        handle.seek(max(0, end - _FOLD_MARKER_BYTES))
        return handle.read()


def _zero_summary(days: list[str]) -> Json:
    """The degraded zeroed summary for an absent or unreadable meter file."""
    return {
        "todayTurns": 0,
        "todayTokens": 0,
        "last7d": [{"date": day, "tokens": 0} for day in days],
        "model": None,
    }


def _fold_event(
    aggregates: dict[str | None, _Aggregate],
    event: Json,
    now: datetime.datetime,
) -> None:
    """Fold one parsed event into the all-provider and its own provider bucket.

    Mirrors the scan's exact bucketing rules: an event whose ``at`` does not
    parse into a day is skipped entirely (no turns, tokens, or model), and
    ``model`` is the most recent matching event's model.
    """
    day = _local_day(event.get("at"), now)
    if day is None:
        return
    tokens = _event_tokens(event)
    provider = event.get("provider")
    keys: tuple[str | None, ...] = (None, provider) if isinstance(provider, str) and provider else (None,)
    for key in keys:
        agg = aggregates.get(key)
        if agg is None:
            agg = aggregates[key] = _Aggregate()
        agg.by_day_tokens[day] = agg.by_day_tokens.get(day, 0) + tokens
        agg.by_day_turns[day] = agg.by_day_turns.get(day, 0) + 1
    model = event.get("model")
    if isinstance(model, str) and model:
        for key in keys:
            aggregates[key].last_model = model


def _summary_from(
    aggregates: dict[str | None, _Aggregate],
    provider: str | None,
    now: datetime.datetime,
    days: list[str],
) -> Json:
    """Render one provider's folded counts into the summary contract shape."""
    agg = aggregates.get(provider)
    if agg is None:
        return _zero_summary(days)
    today = now.date().isoformat()
    return {
        "todayTurns": agg.by_day_turns.get(today, 0),
        "todayTokens": agg.by_day_tokens.get(today, 0),
        "last7d": [{"date": day, "tokens": agg.by_day_tokens.get(day, 0)} for day in days],
        "model": agg.last_model,
    }


def _scan_all(now: datetime.datetime) -> dict[str | None, _Aggregate]:
    """Full rescan of the meter file, rebuilding the fold from scratch.

    Raises OSError on I/O failure; the caller degrades to a zeroed summary.
    """
    global _fold
    path = usage_events_path()
    aggregates: dict[str | None, _Aggregate] = {}
    with open(path, "rb") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            _fold_event(aggregates, event, now)
        offset = handle.tell()
    stat = os.stat(path)
    _fold = _Fold(
        fingerprint=(stat.st_dev, stat.st_ino, offset, stat.st_mtime_ns),
        offset=offset,
        bucketing_tz=now.tzinfo,
        marker=_tail_bytes(path),
        aggregates=aggregates,
    )
    return aggregates


def _fold_tail(now: datetime.datetime) -> dict[str | None, _Aggregate]:
    """Fold only the bytes appended after the stored offset into the fold.

    An append preserves the bytes before the old EOF, so the stored marker is
    re-read at the boundary: a mismatch means the tail segment was rewritten,
    and the whole file must be rescanned. Raises OSError on I/O failure.
    """
    path = usage_events_path()
    with open(path, "rb") as handle:
        handle.seek(_fold.offset)
        tail = handle.read()
        offset = _fold.offset + len(tail)
    if not tail:
        # The file shrank between stat and read; a rescan is the safe answer.
        return _scan_all(now)
    if _fold.marker:
        with open(path, "rb") as handle:
            handle.seek(_fold.offset - len(_fold.marker))
            boundary = handle.read(len(_fold.marker))
        if boundary != _fold.marker:
            return _scan_all(now)
    for raw in tail.splitlines():
        if not raw.strip():
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        _fold_event(_fold.aggregates, event, now)
    stat = os.stat(path)
    _fold.fingerprint = (stat.st_dev, stat.st_ino, offset, stat.st_mtime_ns)
    _fold.offset = offset
    _fold.bucketing_tz = now.tzinfo
    _fold.marker = _tail_bytes(path)
    return _fold.aggregates


def usage_summary(now: datetime.datetime | None = None, provider: str | None = None) -> Json:
    """Aggregate meter events: today's turns/tokens, 7-day bars, last model.

    Days are bucketed in the caller's local timezone so the menu bar's
    "today" matches the calendar day the user sees. ``last7d`` is always
    seven entries (oldest first, including today), zero-filled for quiet
    days, so the UI renders a stable bar list. ``model`` is the model of the
    most recent event, or None when the meter file is absent or empty. When
    ``provider`` is given, only events whose ``provider`` field matches are
    counted (the zen rollup filters on provider="zen").

    Reads are O(1) once the meter file has been folded: a stat fingerprint
    (inode, size, mtime) decides between the cached aggregate, a tail fold of
    appended bytes, and a full rescan when the file shrank or was replaced.
    The fold is updated inside ``record_usage_event`` and guarded by the
    module lock, so concurrent HTTP handlers never observe torn state.
    """
    global _fold
    now = now or datetime.datetime.now().astimezone()
    days = [(now - datetime.timedelta(days=offset)).date().isoformat() for offset in range(6, -1, -1)]
    with _lock:
        try:
            stat = os.stat(usage_events_path())
        except OSError:
            # Meter file absent or unreadable: degrade to zeros, never fail the endpoint.
            _fold = None
            return _zero_summary(days)
        aggregates: dict[str | None, _Aggregate] | None
        if _fold is not None and _same_bucketing_tz(_fold.bucketing_tz, now.tzinfo):
            if _fingerprint(stat) == _fold.fingerprint:
                aggregates = _fold.aggregates
            elif (stat.st_dev, stat.st_ino) == (_fold.fingerprint[0], _fold.fingerprint[1]) and stat.st_size > _fold.offset:
                try:
                    aggregates = _fold_tail(now)
                except OSError:
                    _fold = None
                    aggregates = None
            else:
                try:
                    aggregates = _scan_all(now)
                except OSError:
                    _fold = None
                    aggregates = None
        else:
            # No fold yet, a different bucketing tz, or a replaced file: rescan.
            try:
                aggregates = _scan_all(now)
            except OSError:
                _fold = None
                aggregates = None
        if aggregates is None:
            return _zero_summary(days)
        return _summary_from(aggregates, provider, now, days)


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
                end = handle.tell()
                file_stat = os.fstat(handle.fileno())
        except OSError:
            # Metering is best-effort; never break a live request over it.
            return
        if _fold is not None and (file_stat.st_dev, file_stat.st_ino) == (
            _fold.fingerprint[0], _fold.fingerprint[1]
        ):
            # The fold must be the one built from THIS file (a stale fold from
            # a previous state dir is discarded by the next summary's rescan),
            # and our line must start exactly at the fold's EOF: anything else
            # means bytes we never folded slipped in, so the fold is left as-is
            # and the next summary's tail fold or rescan picks them up.
            line_bytes = len(line.encode("utf-8")) + 1
            if end - line_bytes == _fold.offset:
                try:
                    # Fold the appended event so the next summary is O(1). Day
                    # buckets use the fold's own tz; a summary in a different
                    # tz rescans and re-keys the fold.
                    _fold_event(_fold.aggregates, record, datetime.datetime.now(_fold.bucketing_tz))
                    _fold.fingerprint = (file_stat.st_dev, file_stat.st_ino, end, file_stat.st_mtime_ns)
                    _fold.offset = end
                    _fold.marker = _tail_bytes(path)
                except OSError:
                    # The fold is best-effort too; the next summary recovers it.
                    return
