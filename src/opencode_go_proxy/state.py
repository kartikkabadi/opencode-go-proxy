"""Menu bar state contract (plan 013).

Composes the proxy's local state into one JSON document for the macOS menu
bar: server identity (status/port/upstream), the latest quota snapshot, usage
aggregates from the meter file, and the current model. Everything is
best-effort: a missing or corrupt meter or quota file degrades to zeros or
null rather than failing the endpoint, so the menu bar always gets a stable
shape to render.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

from .meter import usage_events_path
from .protocol import DEFAULT_MODEL
from .quota import read_quota_state

Json = dict[str, Any]

_DAY_KEYS = ("inputTokens", "outputTokens")


def _clean_int(value: Any) -> int | None:
    """Return a clean non-negative int, or None for bools/negative/malformed."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _event_tokens(event: Json) -> int:
    """Tokens for one event: totalTokens when present, else input+output."""
    total = _clean_int(event.get("totalTokens"))
    if total is not None:
        return total
    return sum(_clean_int(event.get(key)) or 0 for key in _DAY_KEYS)


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


def usage_summary(now: datetime.datetime | None = None) -> Json:
    """Aggregate meter events: today's turns/tokens, 7-day bars, last model.

    Days are bucketed in the caller's local timezone so the menu bar's
    "today" matches the calendar day the user sees. ``last7d`` is always
    seven entries (oldest first, including today), zero-filled for quiet
    days, so the UI renders a stable bar list. ``model`` is the model of the
    most recent event, or None when the meter file is absent or empty.
    """
    now = now or datetime.datetime.now().astimezone()
    today = now.date().isoformat()
    days = [(now - datetime.timedelta(days=offset)).date().isoformat() for offset in range(6, -1, -1)]
    by_day: dict[str, int] = {day: 0 for day in days}
    today_turns = 0
    today_tokens = 0
    last_model: str | None = None
    try:
        with open(usage_events_path(), encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                day = _local_day(event.get("at"), now)
                if day is None:
                    continue
                tokens = _event_tokens(event)
                if day in by_day:
                    by_day[day] += tokens
                    if day == today:
                        today_turns += 1
                        today_tokens += tokens
                model = event.get("model")
                if isinstance(model, str) and model:
                    last_model = model
    except OSError:
        pass
    return {
        "todayTurns": today_turns,
        "todayTokens": today_tokens,
        "last7d": [{"date": day, "tokens": by_day[day]} for day in days],
        "model": last_model,
    }


def _sampled_at(value: Any) -> float:
    """Epoch-seconds for a snapshot's sampledAt; 0.0 when unparseable."""
    if not isinstance(value, str):
        return 0.0
    try:
        instant = datetime.datetime.fromisoformat(value)
    except ValueError:
        return 0.0
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=datetime.UTC)
    return instant.timestamp()


def _latest_quota() -> Json | None:
    """Latest provider snapshot by sampledAt, shaped for the contract."""
    state = read_quota_state()
    providers = state.get("providers")
    if not isinstance(providers, dict) or not providers:
        return None
    best: Json | None = None
    best_sampled = -1.0
    for snapshot in providers.values():
        if not isinstance(snapshot, dict):
            continue
        remaining = _clean_int(snapshot.get("remaining"))
        if remaining is None:
            continue
        sampled = _sampled_at(snapshot.get("sampledAt"))
        if sampled <= best_sampled:
            continue
        best_sampled = sampled
        provider = snapshot.get("provider")
        best = {
            "provider": provider if isinstance(provider, str) and provider else "unknown",
            "remaining": remaining,
        }
        limit = _clean_int(snapshot.get("limit"))
        if limit is not None:
            best["limit"] = limit
        reset_at = snapshot.get("resetAt")
        if isinstance(reset_at, str) and reset_at:
            best["resetAt"] = reset_at
    return best


def build_state(port: int, upstream: str, now: datetime.datetime | None = None) -> Json:
    """One stable JSON contract for the menu bar (GET /state)."""
    usage = usage_summary(now)
    model = usage.get("model")
    if not isinstance(model, str) or not model:
        model = DEFAULT_MODEL
    return {
        "status": "ok",
        "port": int(port),
        "upstream": upstream,
        "quota": _latest_quota(),
        "usage": {key: usage[key] for key in ("todayTurns", "todayTokens", "last7d")},
        "model": model,
    }
