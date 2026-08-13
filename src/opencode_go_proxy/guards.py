"""Request guards for the auth transport boundary (plan 006).

Zero-config protection for a loopback listener: reject non-loopback Host
headers (DNS rebinding), reject browser-originated requests, and require a
JSON content type on proxy API requests. The only escape hatch is
``OPENCODE_GO_PROXY_ALLOW_REMOTE=1`` for deliberate non-loopback binds, and
that bypasses the Host check only.
"""

from __future__ import annotations

import os
from http import HTTPStatus

from .errors import ProxyError

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
BROWSER_HEADERS = ("origin", "referer", "sec-fetch-site")


def _host_name(host: str | None) -> str | None:
    """Normalize a Host header to its bare host, dropping the port."""
    if host is None:
        return None
    host = host.strip()
    if not host:
        return None
    if host.startswith("["):  # [::1]:port or bare [::1]
        end = host.find("]")
        return host if end == -1 else host[: end + 1]
    if host.count(":") == 1:  # host:port; unbracketed IPv6 keeps multiple colons
        return host.split(":", 1)[0]
    return host


def check_host(host: str | None) -> None:
    """400 for a missing Host header, 403 for a non-loopback Host."""
    name = _host_name(host)
    if name is None:
        raise ProxyError(HTTPStatus.BAD_REQUEST, "missing Host header", error_type="invalid_host")
    if name not in ALLOWED_HOSTS and os.environ.get("OPENCODE_GO_PROXY_ALLOW_REMOTE") != "1":
        raise ProxyError(HTTPStatus.FORBIDDEN, "request host is not allowed", error_type="invalid_host")


def check_browser_origin(headers) -> None:
    """403 when any browser marker (Origin / Referer / Sec-Fetch-Site) is present."""
    if any(headers.get(h) for h in BROWSER_HEADERS):
        raise ProxyError(
            HTTPStatus.FORBIDDEN,
            "Browser-originated requests are not accepted by the local proxy.",
            error_type="browser_request_rejected",
        )


def check_content_type(content_type: str | None) -> None:
    """415 unless the media type is application/json (params like charset allowed)."""
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media != "application/json":
        raise ProxyError(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            "Proxy requests require Content-Type: application/json.",
            error_type="unsupported_media_type",
        )
