from __future__ import annotations

import threading
from typing import Any

Json = dict[str, Any]


class CacheTracker:
    """Rolling prefix-cache accounting for upstream chat-completions traffic.

    Every proxied request reports how many prompt tokens were served from the
    provider's prefix cache (hit) versus re-processed (miss). The tracker sums
    those per model and exposes the aggregate hit ratio, which is the metric
    that says whether the proxy is producing cache-stable requests.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hit: dict[str, int] = {}
        self._miss: dict[str, int] = {}
        self._requests: dict[str, int] = {}

    def record(self, model: str | None, hit: int, miss: int) -> None:
        if hit < 0 or miss < 0:
            return
        key = model or "unknown"
        with self._lock:
            self._hit[key] = self._hit.get(key, 0) + hit
            self._miss[key] = self._miss.get(key, 0) + miss
            self._requests[key] = self._requests.get(key, 0) + 1

    def snapshot(self) -> Json:
        with self._lock:
            models = sorted(set(self._hit) | set(self._miss))
            per_model = []
            total_hit = 0
            total_miss = 0
            total_requests = 0
            for model in models:
                hit = self._hit.get(model, 0)
                miss = self._miss.get(model, 0)
                total_hit += hit
                total_miss += miss
                total_requests += self._requests.get(model, 0)
                per_model.append(self._row(model, hit, miss, self._requests.get(model, 0)))
            return {
                "models": per_model,
                "totals": self._row("all", total_hit, total_miss, total_requests),
            }

    @staticmethod
    def _row(model: str, hit: int, miss: int, requests: int) -> Json:
        total = hit + miss
        return {
            "model": model,
            "cache_hit_tokens": hit,
            "cache_miss_tokens": miss,
            "requests": requests,
            "hit_ratio": round(hit / total, 6) if total else None,
        }
