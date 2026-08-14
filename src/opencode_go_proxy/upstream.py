"""One upstream chat-completions client with one retry policy.

Every non-streaming upstream call goes through :func:`call_upstream_chat`:
JSON request, bounded retries on transient failures (429 / 5xx / network /
timeout), and a consistent mapping of upstream failures to :class:`ProxyError`.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from typing import Any

from .config import ProxyConfig
from .errors import ProxyError
from .protocol import cache_stats_from_usage
from .quota import record_quota_from_headers
from .secrets import resolve_api_key
from .trace import _mask_trace_body, trace

Json = dict[str, Any]

# Upstream retry policy. Transient failures (429 / 5xx / network / timeout) are
# retried a bounded number of times with a small exponential backoff before the
# error is surfaced, so a flaky upstream does not kill a turn that a retry
# would have completed. Tuned small: localhost proxy, latency-sensitive client.
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BASE_SLEEP_MS = 150

# Budget for one image-caption sub-call. Cached captions return instantly;
# a miss gets 30s and no retries, and a slow/failed caption degrades to a
# placeholder rather than crashing the turn.
DEFAULT_CAPTION_TIMEOUT_SEC = 30.0


def caption_timeout_sec() -> float:
    try:
        return max(1.0, float(os.environ.get("OPENCODE_GO_PROXY_CAPTION_TIMEOUT_SEC", str(DEFAULT_CAPTION_TIMEOUT_SEC))))
    except ValueError:
        return DEFAULT_CAPTION_TIMEOUT_SEC


def default_max_retries() -> int:
    try:
        return max(0, int(os.environ.get("OPENCODE_GO_PROXY_MAX_RETRIES", str(DEFAULT_MAX_RETRIES))))
    except ValueError:
        return DEFAULT_MAX_RETRIES


def retriable_http_status(code: int) -> bool:
    return code in (429, 500, 502, 503, 504)


def retry_sleep(attempt: int) -> None:
    """Sleep for a bounded exponential backoff before retry attempt N (1-based)."""
    base = DEFAULT_RETRY_BASE_SLEEP_MS
    try:
        base = max(0, int(os.environ.get("OPENCODE_GO_PROXY_RETRY_BASE_MS", str(base))))
    except ValueError:
        pass
    delay = min(base * (2 ** (attempt - 1)), 2000) / 1000.0
    time.sleep(delay)


def usage_tokens(usage: Any) -> tuple[Any, Any, Any]:
    """Return (input, output, total) token counts from an upstream usage dict."""
    if not isinstance(usage, dict):
        return None, None, None
    return usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens")


def record_cache(tracker: Any, model: str | None, usage: Any) -> None:
    """Fold one upstream response's cache accounting into the tracker."""
    stats = cache_stats_from_usage(usage)
    if stats["ratio"] is None:
        return
    tracker.record(model or "unknown", stats["hit"], stats["miss"])


def _chat_request(url: str, api_key: str, raw_payload: bytes, accept: str) -> urllib.request.Request:
    """Build the one upstream chat-completions request shape both clients share."""
    return urllib.request.Request(
        url,
        data=raw_payload,
        headers={
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            "accept": accept,
            "user-agent": os.environ.get("OPENCODE_GO_PROXY_USER_AGENT", "codex/1.0"),
        },
        method="POST",
    )


def call_upstream_chat(chat_payload: Json, config: ProxyConfig, request_id: str, *, timeout_sec: float | None = None, max_retries: int | None = None) -> tuple[Json, int]:
    api_key = resolve_api_key(config, request_id)

    url = f"{config.chat_base_url}/chat/completions"
    raw_payload = json.dumps(chat_payload, separators=(",",":")).encode("utf-8")
    max_retries = default_max_retries() if max_retries is None else max_retries
    retries = 0
    while True:
        request = _chat_request(url, api_key, raw_payload, "application/json")
        trace("upstream.start", request_id=request_id, url=url, bytes=len(raw_payload), attempt=retries + 1)
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec if timeout_sec is not None else config.timeout_sec) as response:
                body = response.read()
                record_quota_from_headers(getattr(response, "headers", None))
                elapsed_ms = int((time.time() - started) * 1000)
                trace("upstream.done", request_id=request_id, status=response.status, bytes=len(body), elapsed_ms=elapsed_ms)
                try:
                    value = json.loads(body)
                except json.JSONDecodeError:
                    raise ProxyError(HTTPStatus.BAD_GATEWAY, "upstream returned invalid JSON")
                if not isinstance(value, dict):
                    raise ProxyError(HTTPStatus.BAD_GATEWAY, "upstream returned non-object JSON")
                return value, retries
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            trace("upstream.error", request_id=request_id, status=exc.code, body=_mask_trace_body(body))
            if retriable_http_status(exc.code) and retries < max_retries:
                retries += 1
                trace("upstream.retry", request_id=request_id, attempt=retries, status=exc.code)
                retry_sleep(retries)
                continue
            if exc.code == 429:
                upstream_retry_after = (exc.headers or {}).get("retry-after")
                retry_after = upstream_retry_after or "5"
                raise ProxyError(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    f"rate limited (retry after {retry_after}s)",
                    retries=retries,
                    headers={"retry-after": upstream_retry_after} if upstream_retry_after else None,
                ) from exc
            if exc.code == 503:
                raise ProxyError(HTTPStatus.SERVICE_UNAVAILABLE, "upstream unavailable", retries=retries) from exc
            if exc.code == 504:
                raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "upstream timeout", retries=retries) from exc
            raise ProxyError(
                HTTPStatus.BAD_GATEWAY,
                f"upstream HTTP {exc.code}",
                retries=retries,
                upstream_status=exc.code,
                body=body,
            ) from exc
        except urllib.error.URLError as exc:
            trace("upstream.network_error", request_id=request_id, reason=str(getattr(exc, "reason", exc)))
            if retries < max_retries:
                retries += 1
                trace("upstream.retry", request_id=request_id, attempt=retries, reason=str(getattr(exc, "reason", exc)))
                retry_sleep(retries)
                continue
            raise ProxyError(HTTPStatus.BAD_GATEWAY, f"upstream network error: {getattr(exc, 'reason', exc)}", retries=retries) from exc
        except TimeoutError:
            trace("upstream.timeout", request_id=request_id, timeout=timeout_sec if timeout_sec is not None else config.timeout_sec)
            if retries < max_retries:
                retries += 1
                trace("upstream.retry", request_id=request_id, attempt=retries, reason="timeout")
                retry_sleep(retries)
                continue
            raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "upstream timeout", retries=retries) from None


def call_upstream_chat_verbatim(
    chat_payload: Json, config: ProxyConfig, request_id: str,
    *, timeout_sec: float | None = None, max_retries: int | None = None,
) -> tuple[int, bytes, int, str | None, str | None]:
    """POST chat/completions and return ``(status, raw_body, retries, content_type, retry_after)``.

    The /chat/completions passthrough relay: an upstream HTTP error is returned
    with the upstream's own status and body so the proxy relays it verbatim
    instead of substituting a ``proxy_error`` envelope. Transient failures
    (429 / 5xx) retry under the same policy as :func:`call_upstream_chat`;
    network and timeout failures have no upstream body to relay, so they still
    raise :class:`ProxyError` like the JSON-mapping variant. ``retry_after``
    is the upstream's retry-after header value (lowercased lookup, None when
    absent) so the relay can forward it on rate-limit responses.
    """
    api_key = resolve_api_key(config, request_id)

    url = f"{config.chat_base_url}/chat/completions"
    raw_payload = json.dumps(chat_payload, separators=(",",":")).encode("utf-8")
    max_retries = default_max_retries() if max_retries is None else max_retries
    retries = 0
    while True:
        request = _chat_request(url, api_key, raw_payload, "application/json")
        trace("upstream.start", request_id=request_id, url=url, bytes=len(raw_payload), attempt=retries + 1)
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec if timeout_sec is not None else config.timeout_sec) as response:
                body = response.read()
                record_quota_from_headers(getattr(response, "headers", None))
                elapsed_ms = int((time.time() - started) * 1000)
                trace("upstream.done", request_id=request_id, status=response.status, bytes=len(body), elapsed_ms=elapsed_ms)
                return response.status, body, retries, response.headers.get("content-type"), (response.headers or {}).get("retry-after")
        except urllib.error.HTTPError as exc:
            body = exc.read()
            trace("upstream.error", request_id=request_id, status=exc.code, body=_mask_trace_body(body.decode("utf-8", errors="replace")))
            if retriable_http_status(exc.code) and retries < max_retries:
                retries += 1
                trace("upstream.retry", request_id=request_id, attempt=retries, status=exc.code)
                retry_sleep(retries)
                continue
            return exc.code, body, retries, (exc.headers.get("content-type") if exc.headers else None), (exc.headers or {}).get("retry-after")
        except urllib.error.URLError as exc:
            trace("upstream.network_error", request_id=request_id, reason=str(getattr(exc, "reason", exc)))
            if retries < max_retries:
                retries += 1
                trace("upstream.retry", request_id=request_id, attempt=retries, reason=str(getattr(exc, "reason", exc)))
                retry_sleep(retries)
                continue
            raise ProxyError(HTTPStatus.BAD_GATEWAY, f"upstream network error: {getattr(exc, 'reason', exc)}", retries=retries) from exc
        except TimeoutError:
            trace("upstream.timeout", request_id=request_id, timeout=timeout_sec if timeout_sec is not None else config.timeout_sec)
            if retries < max_retries:
                retries += 1
                trace("upstream.retry", request_id=request_id, attempt=retries, reason="timeout")
                retry_sleep(retries)
                continue
            raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "upstream timeout", retries=retries) from None
