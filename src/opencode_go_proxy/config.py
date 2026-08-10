"""Runtime configuration shared by the proxy modules."""

from __future__ import annotations

from .cache import CacheTracker


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
