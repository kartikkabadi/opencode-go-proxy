"""Rate-limit header harvesting into quota state (plan 011).

Parses OpenAI-style (`x-ratelimit-*`) and Anthropic-style
(`anthropic-ratelimit-*`) response headers from upstream 200 responses into a
quota snapshot per provider, keeps the latest snapshot per provider, and
persists it to ``quota-state.json`` in the state dir with atomic replace.
Harvesting is best-effort like the usage meter: quota recording must never
break a live request, so I/O errors are swallowed and a headerless upstream
degrades to an empty snapshot.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
import time
from typing import Any

from .meter import state_dir

Json = dict[str, Any]

# A bare numeric reset is ambiguous: OpenAI-style proxies historically sent an
# epoch instant, while some send "seconds until reset". Values at or above this
# threshold are treated as epoch instants; smaller values as seconds-from-now.
_EPOCH_THRESHOLD = 10**9

_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}

# Per-provider header families in priority order; the first family with a
# parseable `remaining` wins for that provider.
_OPENAI_FAMILIES = (
    ("requests", "x-ratelimit-{part}-requests"),
    ("tokens", "x-ratelimit-{part}-tokens"),
    ("", "x-ratelimit-{part}"),
)
_ANTHROPIC_FAMILIES = (
    ("requests", "anthropic-ratelimit-requests-{part}"),
    ("input-tokens", "anthropic-ratelimit-input-tokens-{part}"),
    ("output-tokens", "anthropic-ratelimit-output-tokens-{part}"),
    ("tokens", "anthropic-ratelimit-tokens-{part}"),
)

_lock = threading.Lock()


def quota_state_path() -> str:
    return os.path.join(state_dir(), "quota-state.json")


def _headers_map(headers: Any) -> Any:
    """Return a case-insensitive ``.get()`` source for dict or HTTP headers."""
    if headers is None:
        return {}
    if isinstance(headers, dict):
        return {str(k).lower(): str(v) for k, v in headers.items()}
    # http.client.HTTPMessage.get() is already case-insensitive.
    return headers


def _int_value(value: Any) -> int | None:
    """Return a clean non-negative int, or None for bools/negative/malformed."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    if isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _parse_duration(text: str) -> float | None:
    """Parse OpenAI reset durations like ``1s``, ``6m0s``, or ``2h30m``."""
    tokens = _DURATION_TOKEN.findall(text)
    if not tokens or "".join(f"{number}{unit}" for number, unit in tokens) != text:
        return None
    return sum(float(number) * _UNIT_SECONDS[unit] for number, unit in tokens)


def _parse_reset_epoch(value: Any) -> float | None:
    """Convert a reset header to epoch seconds, tolerating common encodings."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        instant = datetime.datetime.fromisoformat(text)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=datetime.UTC)
        return instant.timestamp()
    except ValueError:
        pass
    duration = _parse_duration(text)
    if duration is not None:
        return time.time() + duration
    try:
        number = float(text)
    except ValueError:
        return None
    if number < 0:
        return None
    return number if number >= _EPOCH_THRESHOLD else time.time() + number


def _iso_at(value: float | None) -> str:
    """ISO 8601 UTC instant (Z) for an epoch-seconds value, or now."""
    if value is None:
        instant = datetime.datetime.now(datetime.UTC)
    else:
        instant = datetime.datetime.fromtimestamp(value, tz=datetime.UTC)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _snapshot(provider: str, limit: Any, remaining: Any, reset: Any) -> Json | None:
    """Build one quota snapshot; None when no usable `remaining` is present."""
    remaining_clean = _int_value(remaining)
    if remaining_clean is None:
        return None
    snapshot: Json = {
        "provider": provider,
        "remaining": remaining_clean,
        "sampledAt": _iso_at(None),
    }
    limit_clean = _int_value(limit)
    if limit_clean is not None:
        snapshot["limit"] = limit_clean
    reset_epoch = _parse_reset_epoch(reset)
    if reset_epoch is not None:
        snapshot["resetAt"] = _iso_at(reset_epoch)
    return snapshot


def _family_snapshot(lookup: Any, provider: str, families: tuple[tuple[str, str], ...]) -> Json | None:
    """Return the first family's snapshot with a parseable `remaining`."""
    for _label, template in families:
        snapshot = _snapshot(
            provider,
            limit=lookup.get(template.format(part="limit")),
            remaining=lookup.get(template.format(part="remaining")),
            reset=lookup.get(template.format(part="reset")),
        )
        if snapshot is not None:
            return snapshot
    return None


def quota_snapshot_from_headers(headers: Any) -> dict[str, Json]:
    """Parse upstream response headers into ``{provider: snapshot}``."""
    lookup = _headers_map(headers)
    snapshots: dict[str, Json] = {}
    for provider, families in (("openai", _OPENAI_FAMILIES), ("anthropic", _ANTHROPIC_FAMILIES)):
        snapshot = _family_snapshot(lookup, provider, families)
        if snapshot is not None:
            snapshots[provider] = snapshot
    return snapshots


def read_quota_state() -> Json:
    """Read quota-state.json; an absent or corrupt file yields an empty state."""
    try:
        with open(quota_state_path(), encoding="utf-8") as handle:
            value = json.load(handle)
        if isinstance(value, dict) and isinstance(value.get("providers"), dict):
            return value
    except (OSError, ValueError):
        pass
    return {"providers": {}}


def _write_state(state: Json) -> None:
    path = quota_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def record_quota_from_headers(headers: Any) -> None:
    """Parse response headers and persist the latest snapshot per provider."""
    snapshots = quota_snapshot_from_headers(headers)
    if not snapshots:
        return
    with _lock:
        try:
            state = read_quota_state()
            state["providers"].update(snapshots)
            _write_state(state)
        except OSError:
            # Quota harvesting is best-effort; never break a live request.
            pass
