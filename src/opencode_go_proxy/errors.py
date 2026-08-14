"""Shared error type for proxy-side failures surfaced to the client."""

from __future__ import annotations

from http import HTTPStatus


class ProxyError(Exception):
    def __init__(
        self,
        status: HTTPStatus,
        message: str,
        *,
        retries: int = 0,
        upstream_status: int | None = None,
        error_type: str | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retries = retries
        # The upstream's own status when the proxy surfaces a different one
        # (e.g. upstream 400 relayed as 502) so callers can still tell
        # "model/detail rejected" from "upstream is broken".
        self.upstream_status = upstream_status
        # Rendered as the error type in the JSON envelope; defaults to
        # "proxy_error" at dispatch time.
        self.error_type = error_type
        # Upstream response headers the proxy forwards on its own error
        # response (e.g. retry-after on 429); keys are lowercase.
        self.headers = headers
        # The upstream's error body (decoded text or raw bytes) when the proxy
        # surfaces a different status; None when no body was captured. Callers
        # that must inspect the upstream payload before rendering (e.g. the
        # go ModelError "not supported" zen fallback) read it here.
        self.body = body
