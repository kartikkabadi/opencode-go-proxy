"""Runtime configuration shared by the proxy modules."""

from __future__ import annotations

import os

from .cache import CacheTracker

DEFAULT_CHAT_BASE_URL = "https://opencode.ai/zen/go/v1"


def resolve_chat_base_url(explicit: str | None = None) -> str:
    """Upstream chat base URL: explicit flag, then env overrides, then default.

    Precedence is OPENCODE_GO_BASE_URL, then OPENCODE_ZEN_BASE_URL, then the
    legacy CHAT_COMPLETIONS_BASE_URL (kept so existing installs do not break),
    then the built-in default.
    """
    if explicit:
        return explicit.rstrip("/")
    for name in ("OPENCODE_GO_BASE_URL", "OPENCODE_ZEN_BASE_URL", "CHAT_COMPLETIONS_BASE_URL"):
        value = os.environ.get(name)
        if value:
            return value.rstrip("/")
    return DEFAULT_CHAT_BASE_URL


class ProxyConfig:
    def __init__(
        self,
        *,
        bind: str,
        port: int,
        chat_base_url: str,
        api_key_env: str,
        timeout_sec: float,
        max_body_bytes: int,
    ) -> None:
        self.bind = bind
        self.port = port
        self.chat_base_url = chat_base_url.rstrip("/")
        self.api_key_env = api_key_env
        self.timeout_sec = timeout_sec
        self.max_body_bytes = max_body_bytes
        self.cache_tracker = CacheTracker()
