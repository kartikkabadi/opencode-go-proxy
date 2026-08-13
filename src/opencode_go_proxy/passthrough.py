"""Verbatim relay of native-model requests to the ChatGPT Codex backend.

Native turns are forwarded whole - body, status, stream - with the client's
own authorization and x-* headers. The OpenCode Go key is never attached, and
native turns meter under provider="native" so they never count against the
opencode-go quota.
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.error
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
NATIVE_PROVIDER = "native"

# The fixed headers relayed from the client request, plus every x-* header
# that is not an x-opencode-go-* proxy control header.
FORWARD_HEADERS = ("authorization", "content-type", "accept", "user-agent")


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


def _relay_status(handler: Any, status: int, content_type: str, body: bytes) -> None:
    handler.send_response(status)
    handler.send_header("content-type", content_type)
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


def _relay_stream(response: Any, handler: Any, request_id: str) -> None:
    """Relay upstream SSE lines verbatim with a keepalive comment thread.

    Client-disconnect and stream-abort handling mirror
    handle_chat_stream_passthrough: the relay stops as soon as the client
    disconnects, and an upstream error after the 200 head is only traced.
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
    try:
        with response as resp:
            for line in resp:
                if not client_alive:
                    break
                try:
                    with write_lock:
                        handler.wfile.write(line)
                        handler.wfile.flush()
                except (BrokenPipeError, OSError):
                    client_alive = False
                    trace(
                        "client.disconnected",
                        request_id=request_id,
                        message="client closed connection during stream",
                    )
                    break
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        trace(
            "upstream.stream_aborted",
            request_id=request_id,
            status=getattr(exc, "code", None) or getattr(exc, "reason", str(exc)),
        )
    finally:
        keepalive_stop.set()


def relay_native_request(handler: Any, payload: Json, config: ProxyConfig, request_id: str) -> None:
    """POST the payload to {base}/v1/responses and relay the response verbatim.

    Non-stream: upstream status + raw body + content-type unchanged. Stream:
    the SSE head is committed only after the backend answers 200, then every
    line is relayed unchanged. A non-200 upstream answer is relayed with its
    own status and body before any SSE is committed.
    """
    started = time.time()
    model = payload.get("model") or "unknown"
    base_url = resolve_native_base_url()
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
        _relay_status(handler, exc.code, content_type, body)
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

    if payload.get("stream") is True:
        handler.send_response(HTTPStatus.OK)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("cache-control", "no-cache")
        handler.end_headers()
        _relay_stream(response, handler, request_id)
    else:
        body = response.read()
        content_type = response.headers.get("content-type") or "application/json"
        _relay_status(handler, response.status, content_type, body)

    elapsed_ms = int((time.time() - started) * 1000)
    trace("native.done", request_id=request_id, status=response.status, elapsed_ms=elapsed_ms)
    record_usage_event(
        model=model, status=response.status,
        duration_ms=elapsed_ms, provider=NATIVE_PROVIDER,
    )
