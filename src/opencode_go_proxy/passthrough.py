"""Semantically unchanged relay of native-model requests to the ChatGPT Codex backend.

The JSON payload is parsed and re-serialized (whitespace and key order are
not preserved), the client's auth and request headers are forwarded, safe
upstream response headers are relayed back, and the backend base URL is
required to be https unless the local-development escape hatch is set.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from typing import Any

from .config import ProxyConfig
from .errors import ProxyError
from .meter import record_usage_event
from .streaming import keepalive_sec
from .trace import trace

Json = dict[str, Any]

NATIVE_BASE_URL_DEFAULT = "https://chatgpt.com/backend-api/codex"
NATIVE_BASE_URL_ENV = "OPENCODE_GO_PROXY_NATIVE_BASE_URL"
NATIVE_INSECURE_ENV = "OPENCODE_GO_PROXY_NATIVE_ALLOW_INSECURE"
NATIVE_PROVIDER = "native"

# The fixed headers relayed from the client request, plus every x-* header
# that is not an x-opencode-go-* proxy control header.
FORWARD_HEADERS = ("authorization", "content-type", "accept", "user-agent")

# Headers that must never be forwarded end-to-end (RFC 9110 hop-by-hop plus
# anything a relay owns itself).
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


def resolve_native_base_url(explicit: str | None = None) -> str:
    """Native backend base URL: explicit value, env override, then default."""
    if explicit:
        return explicit.rstrip("/")
    value = os.environ.get(NATIVE_BASE_URL_ENV)
    if value:
        return value.rstrip("/")
    return NATIVE_BASE_URL_DEFAULT


def _forwarded_headers(handler: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    for name in FORWARD_HEADERS:
        value = handler.headers.get(name)
        if value:
            headers[name] = value
    for name, value in handler.headers.items():
        lowered = name.lower()
        if lowered.startswith("x-") and not lowered.startswith("x-opencode-go-"):
            headers[lowered] = value
    return headers


def _require_https(base_url: str) -> None:
    """Refuse to relay the client's Authorization header to a plain-http backend."""
    scheme = urllib.parse.urlsplit(base_url).scheme.lower()
    if scheme != "https" and os.environ.get(NATIVE_INSECURE_ENV) != "1":
        raise ProxyError(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            f"native backend must use https (got {base_url!r}); "
            f"set {NATIVE_INSECURE_ENV}=1 only for local testing",
        )


def _relay_response_headers(handler: Any, headers: Any, *, streaming: bool) -> None:
    """Forward safe upstream response headers; owned and hop-by-hop ones are skipped."""
    skip = HOP_BY_HOP_HEADERS | {"content-type"}
    if streaming:
        skip |= {"content-length", "cache-control"}
    for name, value in headers.items():
        if name.lower() not in skip:
            handler.send_header(name, value)


def _relay_status(handler: Any, status: int, content_type: str, body: bytes, headers: Any = None) -> None:
    handler.send_response(status)
    if headers is not None:
        _relay_response_headers(handler, headers, streaming=False)
    handler.send_header("content-type", content_type)
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()

def _relay_stream(response: Any, handler: Any, request_id: str) -> str:
    """Relay upstream SSE lines with a keepalive comment thread.

    Returns the outcome: "done" when the upstream stream reached its terminal
    marker ([DONE] or response.completed), "aborted" when it ended without
    one (upstream error or truncation), "gone" when the client disconnected.
    The 200 head is already committed by the caller in every case, so the
    meter must record the outcome rather than a naive 200.
    """
    keepalive_stop = threading.Event()
    write_lock = threading.Lock()
    interval = keepalive_sec()
    client_alive = True

    def keepalive() -> None:
        nonlocal client_alive
        while not keepalive_stop.wait(interval):
            if not client_alive:
                return
            try:
                with write_lock:
                    handler.wfile.write(b": keepalive\n\n")
                    handler.wfile.flush()
            except (BrokenPipeError, OSError):
                client_alive = False
                return

    ka_thread = threading.Thread(target=keepalive, daemon=True)
    ka_thread.start()
    outcome = "aborted"
    try:
        with response as resp:
            for line in resp:
                if not client_alive:
                    outcome = "gone"
                    break
                try:
                    with write_lock:
                        handler.wfile.write(line)
                        handler.wfile.flush()
                except (BrokenPipeError, OSError):
                    client_alive = False
                    outcome = "gone"
                    trace(
                        "client.disconnected",
                        request_id=request_id,
                        message="client closed connection during stream",
                    )
                    break
                if b"[DONE]" in line or b'"type":"response.completed"' in line:
                    outcome = "done"
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        outcome = "aborted"
        trace(
            "upstream.stream_aborted",
            request_id=request_id,
            status=getattr(exc, "code", None) or getattr(exc, "reason", str(exc)),
        )
    finally:
        keepalive_stop.set()
    return outcome


def relay_native_request(handler: Any, payload: Json, config: ProxyConfig, request_id: str) -> None:
    """POST the payload to {base}/v1/responses and relay the response.

    The JSON payload is sent semantically unchanged (parsed and re-serialized;
    whitespace and key order are not preserved). Non-stream: upstream status,
    safe headers, and raw body. Stream: the SSE head is committed only after
    the backend answers 200, then every line is relayed unchanged. A non-200
    upstream answer is relayed with its own status and body before any SSE is
    committed. The backend base URL must be https unless the local-testing
    escape hatch is set.
    """
    started = time.time()
    model = payload.get("model") or "unknown"
    base_url = resolve_native_base_url()
    _require_https(base_url)
    url = f"{base_url}/v1/responses"
    raw_payload = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=raw_payload,
        headers=_forwarded_headers(handler),
        method="POST",
    )
    trace(
        "native.start",
        request_id=request_id,
        url=url,
        bytes=len(raw_payload),
        stream=payload.get("stream") is True,
    )
    try:
        response = urllib.request.urlopen(request, timeout=config.timeout_sec)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        content_type = (exc.headers.get("content-type") if exc.headers else None) or "application/json"
        _relay_status(handler, exc.code, content_type, body, exc.headers)
        record_usage_event(
            model=model, status=exc.code,
            duration_ms=int((time.time() - started) * 1000), provider=NATIVE_PROVIDER,
        )
        return
    except (urllib.error.URLError, TimeoutError) as exc:
        status = HTTPStatus.GATEWAY_TIMEOUT if isinstance(exc, TimeoutError) else HTTPStatus.BAD_GATEWAY
        record_usage_event(
            model=model, status=int(status),
            duration_ms=int((time.time() - started) * 1000), provider=NATIVE_PROVIDER,
        )
        if isinstance(exc, TimeoutError):
            raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "native backend timeout") from exc
        raise ProxyError(HTTPStatus.BAD_GATEWAY, f"native backend network error: {getattr(exc, 'reason', exc)}") from exc

    elapsed_ms = int((time.time() - started) * 1000)
    if payload.get("stream") is True:
        handler.send_response(HTTPStatus.OK)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("cache-control", "no-cache")
        _relay_response_headers(handler, response.headers, streaming=True)
        handler.end_headers()
        outcome = _relay_stream(response, handler, request_id)
        if outcome == "aborted":
            trace("native.done", request_id=request_id, status=502, outcome=outcome, elapsed_ms=elapsed_ms)
            record_usage_event(
                model=model, status=502, duration_ms=elapsed_ms,
                stream_aborted=True, provider=NATIVE_PROVIDER,
            )
            return
        if outcome == "gone":
            trace("native.done", request_id=request_id, status=0, outcome=outcome, elapsed_ms=elapsed_ms)
            record_usage_event(
                model=model, status=0, duration_ms=elapsed_ms,
                stream_aborted=True, provider=NATIVE_PROVIDER,
            )
            return
        trace("native.done", request_id=request_id, status=200, outcome=outcome, elapsed_ms=elapsed_ms)
        record_usage_event(
            model=model, status=200, duration_ms=elapsed_ms, provider=NATIVE_PROVIDER,
        )
        return

    body = response.read()
    content_type = response.headers.get("content-type") or "application/json"
    _relay_status(handler, response.status, content_type, body, response.headers)
    trace("native.done", request_id=request_id, status=response.status, elapsed_ms=elapsed_ms)
    record_usage_event(
        model=model, status=response.status,
        duration_ms=elapsed_ms, provider=NATIVE_PROVIDER,
    )
