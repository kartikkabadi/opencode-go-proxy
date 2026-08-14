"""Menu bar state contract (plan 013).

Composes the proxy's local state into one JSON document for the macOS menu
bar: server identity (status/port/upstream), the latest quota snapshot, usage
aggregates from the meter file, the polled OpenCode Go plan usage, and the
current model. Everything is best-effort: a missing or corrupt meter or quota
file, or an unreachable usage endpoint, degrades to zeros or null rather than
failing the endpoint, so the menu bar always gets a stable shape to render.
"""

from __future__ import annotations

import datetime
from typing import Any

from . import __version__
from .meter import usage_summary
from .protocol import DEFAULT_MODEL
from .quota import read_quota_state
from .updates import version_payload
from .usage_poller import poll_go_usage

Json = dict[str, Any]

# Dollar budgets for the OpenCode Go plan, rendered next to live usage.
GO_LIMITS: Json = {
    "monthlyDollars": 60,
    "weeklyDollars": 30,
    "rolling5hDollars": 12,
    "subscriptionMonthlyDollars": 10,
}


def _clean_int(value: Any) -> int | None:
    """Return a clean non-negative int, or None for bools/negative/malformed."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _sampled_at(value: Any) -> float | None:
    """Epoch-seconds for a snapshot's sampledAt; None when unparseable."""
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
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
        if sampled is None or sampled <= best_sampled:
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


def _zen_rollup(now: datetime.datetime | None = None) -> Json:
    """Zen-only usage: today's turns/tokens and seven daily token sums."""
    summary = usage_summary(now, provider="zen")
    return {
        "todayTurns": summary["todayTurns"],
        "todayTokens": summary["todayTokens"],
        "last7d": [day["tokens"] for day in summary["last7d"]],
    }


def build_state(port: int, upstream: str, now: datetime.datetime | None = None) -> Json:
    """One stable JSON contract for the menu bar (GET /state).

    ``usage`` keeps the legacy all-provider keys (todayTurns, todayTokens,
    last7d) and adds the Go plan slot (null when the poller cannot fetch it),
    the fixed Go dollar budgets, and the zen-only rollup. ``version`` and
    ``update`` come from the daily-TTL GitHub release check; the check never
    fails the endpoint.
    """
    usage = usage_summary(now)
    model = usage.get("model")
    if not isinstance(model, str) or not model:
        model = DEFAULT_MODEL
    try:
        update = version_payload()["update"]
    except Exception as exc:  # noqa: BLE001 - /state must always render its shape
        update = {
            "available": False,
            "checked_at": None,
            "error": f"update check failed: {exc}",
            "latest": None,
            "release_url": None,
        }
    return {
        "status": "ok",
        "port": int(port),
        "upstream": upstream,
        "quota": _latest_quota(),
        "usage": {
            "todayTurns": usage["todayTurns"],
            "todayTokens": usage["todayTokens"],
            "last7d": usage["last7d"],
            "go": poll_go_usage(),
            "goLimits": dict(GO_LIMITS),
            "zen": _zen_rollup(now),
        },
        "model": model,
        "version": __version__,
        "update": update,
    }
