"""Poll the OpenCode Go plan usage endpoint for the menu bar.

Fetches rolling/weekly/monthly usage from opencode.ai with the same API key
the proxy uses for upstream traffic, cached in memory for a minute so /state
never blocks on the network more than once a minute. Best-effort by
contract: any failure (missing key, HTTP error, timeout, malformed body)
returns None and never raises, so /state always keeps its stable shape.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import Any

from .config import ProxyConfig, resolve_chat_base_url
from .errors import ProxyError
from .secrets import configured_key_env, resolve_api_key

Json = dict[str, Any]

USAGE_URL_ENV = "OPENCODE_GO_USAGE_URL"
DEFAULT_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
POLL_TIMEOUT_SEC = 5
TTL_SECONDS = 60

# Defaults mirrored from ops._proxy_config() for key resolution when no
# caller supplies a config (the /state handler passes none today).
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_TIMEOUT_SEC = 180
DEFAULT_MAX_BODY_BYTES = 20 * 1024 * 1024

# Trace label for key resolution, distinct from request-scoped labels.
_REQUEST_ID = "usage-poll"

_lock = threading.Lock()
# (monotonic fetch time, parsed usage dict); set only on successful fetches.
_cache: tuple[float, Json] | None = None


def usage_url() -> str:
    """The usage endpoint the poller hits (env override for tests)."""
    return os.environ.get(USAGE_URL_ENV) or DEFAULT_USAGE_URL


def clear_cache() -> None:
    """Drop the TTL cache (test isolation only)."""
    global _cache
    with _lock:
        _cache = None


def poll_go_usage(config: ProxyConfig | None = None) -> Json | None:
    """Return OpenCode Go usage, fresh or from the TTL cache; None on failure.

    The returned shape is {"rolling": {...}, "weekly": {...}, "monthly": {...}}.
    Never raises: a missing key, HTTP error, timeout, or malformed body all
    degrade to None so callers can render a null slot. With no config the
    poller resolves the key with the proxy's default env (and keychain).
    """
    global _cache
    now = time.monotonic()
    with _lock:
        cached = _cache
    if cached is not None and now - cached[0] < TTL_SECONDS:
        return cached[1]
    usage = _fetch_usage(config if config is not None else _default_config())
    if usage is not None:
        with _lock:
            _cache = (time.monotonic(), usage)
    return usage


def _default_config() -> ProxyConfig:
    """A ProxyConfig for key resolution when the caller supplies none."""
    return ProxyConfig(
        bind=DEFAULT_BIND,
        port=DEFAULT_PORT,
        chat_base_url=resolve_chat_base_url(),
        api_key_env=configured_key_env(),
        timeout_sec=DEFAULT_TIMEOUT_SEC,
        max_body_bytes=DEFAULT_MAX_BODY_BYTES,
    )


def _fetch_usage(config: ProxyConfig) -> Json | None:
    """One uncached GET of the usage endpoint; None on any failure."""
    try:
        api_key = resolve_api_key(config, _REQUEST_ID)
    except ProxyError:
        # No resolvable key: usage is unknowable, degrade to null.
        return None
    request = urllib.request.Request(
        usage_url(),
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=POLL_TIMEOUT_SEC) as response:
            body = response.read()
    except OSError:
        # URLError (incl. HTTPError) and socket.timeout are OSError subclasses.
        return None
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    return usage
