"""Shared error type for proxy-side failures surfaced to the client."""

from __future__ import annotations

from http import HTTPStatus


class ProxyError(Exception):
    def __init__(self, status: HTTPStatus, message: str, *, retries: int = 0, upstream_status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retries = retries
        # The upstream's own status when the proxy surfaces a different one
        # (e.g. upstream 400 relayed as 502) so callers can still tell
        # "model/detail rejected" from "upstream is broken".
        self.upstream_status = upstream_status
