from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__, vision
from .cache import CacheTracker
from .meter import record_usage_event
from .protocol import (
    DEFAULT_MODEL,
    IMAGE_MODEL_DEFAULT,
    KNOWN_MODELS,
    cache_stats_from_usage,
    chat_completion_to_response,
    chat_message_to_response_output,
    inject_session_model,
    new_response_id,
    normalize_usage,
    now_unix,
    responses_payload_to_chat_payload,
)

Json = dict[str, Any]

# Secret-looking content masked out of upstream error bodies before they are
# traced to stderr (and later shipped in a support bundle). An upstream that
# echoes the request Authorization header back in its error body would
# otherwise leak the API key into logs verbatim.
_MASK_RE = re.compile(
    r"(?i)(authorization[\s\"']*[:=][\s\"']*)[^\"'\r\n,;]+|(\bsk-[A-Za-z0-9_\-]{8,}\b)"
)


def _mask_trace_body(body: str, limit: int = 2000) -> str:
    text = body[:limit]

    def _repl(m: re.Match) -> str:
        if m.group(1):
            return f"{m.group(1)}<redacted>"
        return "<redacted>"

    return _MASK_RE.sub(_repl, text)


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


def trace(event: str, **fields: Any) -> None:
    record = {"ts": time.time(), "event": event, **fields}
    print(json.dumps(record, sort_keys=True), file=sys.stderr, flush=True)


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


def _caption_timeout_sec() -> float:
    try:
        return max(1.0, float(os.environ.get("OPENCODE_GO_PROXY_CAPTION_TIMEOUT_SEC", str(DEFAULT_CAPTION_TIMEOUT_SEC))))
    except ValueError:
        return DEFAULT_CAPTION_TIMEOUT_SEC


def _max_retries() -> int:
    try:
        return max(0, int(os.environ.get("OPENCODE_GO_PROXY_MAX_RETRIES", str(DEFAULT_MAX_RETRIES))))
    except ValueError:
        return DEFAULT_MAX_RETRIES


def _retriable_http_status(code: int) -> bool:
    return code in (429, 500, 502, 503, 504)


def _retry_sleep(attempt: int) -> None:
    """Sleep for a bounded exponential backoff before retry attempt N (1-based)."""
    base = DEFAULT_RETRY_BASE_SLEEP_MS
    try:
        base = max(0, int(os.environ.get("OPENCODE_GO_PROXY_RETRY_BASE_MS", str(base))))
    except ValueError:
        pass
    delay = min(base * (2 ** (attempt - 1)), 2000) / 1000.0
    time.sleep(delay)


def _usage_tokens(usage: Any) -> tuple[Any, Any, Any]:
    """Return (input, output, total) token counts from an upstream usage dict."""
    if not isinstance(usage, dict):
        return None, None, None
    return usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens")


def record_cache(tracker: CacheTracker, model: str | None, usage: Any) -> None:
    """Fold one upstream response's cache accounting into the tracker."""
    stats = cache_stats_from_usage(usage)
    if stats["ratio"] is None:
        return
    tracker.record(model or "unknown", stats["hit"], stats["miss"])


def _decompress_bounded(reader: Any, cap: int) -> bytes:
    """Read `reader` in chunks, raising 413 once the output exceeds `cap`.

    Both zstd and gzip frames can declare an output size that is only a hint,
    so a zip bomb must be bounded by counting bytes as they come out, never by
    trusting the frame header or decompressing the whole body at once.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = reader.read(1 << 16)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise ProxyError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "decompressed request body exceeds the size cap",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def decode_request_body(raw: bytes, content_encoding: str, max_body_bytes: int) -> bytes:
    """Decode an HTTP request body according to its Content-Encoding header.

    The Codex desktop app sends /v1/responses bodies zstd-compressed whenever it
    is authenticated (codex-rs: `Compression::Zstd`), so a proxy that serves the
    app must decompress. gzip is supported as well; anything else is rejected
    explicitly rather than crashing on a magic byte.
    """
    encoding = (content_encoding or "").strip().lower()
    if not encoding or encoding in {"identity", "utf-8"}:
        return raw
    if encoding == "zstd":
        try:
            import zstandard as zstd
        except ImportError as exc:  # pragma: no cover - env without the dep
            raise ProxyError(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "zstd request bodies require the 'zstandard' package (pip install zstandard)",
            ) from exc
        cap = max(max_body_bytes * 4, 1 << 20)
        try:
            with zstd.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as reader:
                return _decompress_bounded(reader, cap)
        except ProxyError:
            raise
        except Exception as exc:
            raise ProxyError(HTTPStatus.BAD_REQUEST, f"failed to decompress zstd request body: {exc}") from exc
    if encoding in {"gzip", "x-gzip"}:
        # Same bounded decompression as zstd: a tiny gzip wire body must not be
        # able to balloon into a multi-GB allocation (gzip.decompress has no cap).
        cap = max(max_body_bytes * 4, 1 << 20)
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as reader:
                return _decompress_bounded(reader, cap)
        except ProxyError:
            raise
        except Exception as exc:
            raise ProxyError(HTTPStatus.BAD_REQUEST, f"failed to decompress gzip request body: {exc}") from exc
    raise ProxyError(HTTPStatus.BAD_REQUEST, f"unsupported content-encoding: {content_encoding}")


class ResponsesProxyHandler(BaseHTTPRequestHandler):
    def _reject_websocket_upgrade(self) -> bool:
        """Reject a realtime WebSocket upgrade with HTTP/1.1 426.

        The Codex app opens a WebSocket upgrade for realtime; BaseHTTPRequestHandler
        defaults to HTTP/1.0 and answers with an HTTP/1.0 status line, which the client
        rejects as "WebSocket protocol error: HTTP version must be 1.1 or higher".
        Answering the raw upgrade with HTTP/1.1 426 Upgrade Required makes the client
        fall back to plain HTTP streaming cleanly (mirrors codex-router src/router.mjs).
        """
        connection = (self.headers.get("Connection") or "").lower()
        upgrade = (self.headers.get("Upgrade") or "").lower()
        if "upgrade" not in connection or upgrade != "websocket":
            return False
        self.wfile.write(
            b"HTTP/1.1 426 Upgrade Required\r\n"
            b"Connection: close\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        self.wfile.flush()
        self.close_connection = True
        return True

    def do_GET(self) -> None:
        if self._reject_websocket_upgrade():
            return
        if self.path in {"/health", "/v1/health"}:
            self._send_json({"status": "ok"})
            return
        if self.path in {"/cache", "/v1/cache", "/metrics", "/v1/metrics"}:
            config: ProxyConfig = self.server.config  # type: ignore[attr-defined]
            self._send_json(config.cache_tracker.snapshot())
            return
        if self.path in {"/models", "/v1/models"}:
            self._send_json({
                "object": "list",
                "data": [{"id": slug, "object": "model"} for slug in sorted(KNOWN_MODELS)],
            })
            return
        self._send_json({"error": {"message": "not found"}}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        request_id = uuid.uuid4().hex[:12]
        # /responses/compact is a standard Responses request; reuse the same handler.
        if self.path not in {"/responses", "/v1/responses", "/responses/compact", "/v1/responses/compact"}:
            self._send_json({"error": {"message": "not found"}}, status=HTTPStatus.NOT_FOUND)
            return

        try:
            config: ProxyConfig = self.server.config  # type: ignore[attr-defined]
            payload = self._read_json(config)
            trace(
                "request.received",
                request_id=request_id,
                path=self.path,
                model=payload.get("model"),
                stream=payload.get("stream", False),
            )
            if payload.get("stream") is True:
                # Real streaming: send SSE headers, then stream from upstream in real-time.
                self.send_response(HTTPStatus.OK)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.end_headers()
                try:
                    handle_streaming_request(payload, config, request_id, self.wfile)
                except Exception as exc:  # noqa: BLE001 - defensive crash trace
                    trace("request.crashed", request_id=request_id, message=str(exc), traceback=traceback.format_exc())
                    try:
                        err = json.dumps({"type": "response.error", "error": {"message": "proxy crashed; see stderr trace"}}, separators=(",",":")).encode("utf-8")
                        self.wfile.write(b"data: " + err + b"\n\ndata: [DONE]\n\n")
                        self.wfile.flush()
                    except BrokenPipeError:
                        pass
            else:
                response = handle_responses_request(payload, config, request_id)
                self._send_json(response)
        except ProxyError as exc:
            trace("request.failed", request_id=request_id, status=exc.status, message=exc.message)
            self._send_json({"error": {"message": exc.message, "type": "proxy_error"}}, status=exc.status)
        except BrokenPipeError:
            trace("client.disconnected", request_id=request_id, message="client closed connection during stream")
        except Exception as exc:  # pragma: no cover - defensive crash trace  # noqa: BLE001
            trace("request.crashed", request_id=request_id, message=str(exc), traceback=traceback.format_exc())
            try:
                self._send_json(
                    {"error": {"message": "proxy crashed; see stderr trace", "type": "proxy_crash"}},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            except BrokenPipeError:
                pass

    def _read_json(self, config: ProxyConfig) -> Json:
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            raise ProxyError(HTTPStatus.BAD_REQUEST, "invalid content-length header")
        if length < 0:
            raise ProxyError(HTTPStatus.BAD_REQUEST, "negative content-length")
        if length > config.max_body_bytes:
            raise ProxyError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"request body exceeds {config.max_body_bytes // (1024*1024)}MB cap")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        body = decode_request_body(raw, self.headers.get("content-encoding", ""), config.max_body_bytes)
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProxyError(HTTPStatus.BAD_REQUEST, "request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ProxyError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
        return value

    def _send_json(self, payload: Json, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, separators=(",",":")).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


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


def handle_responses_request(payload: Json, config: ProxyConfig, request_id: str) -> Json:
    started = time.time()
    session_model = payload.get("model") or DEFAULT_MODEL
    payload = inject_session_model(payload, session_model)
    chat_payload, request_model, conversion_stats = responses_payload_to_chat_payload(payload)

    # Split-turn: if image + tools, caption images via a vision sub-call, then route to the requested model.
    # MiMo can't drive tool loops from tool-role image messages; caption + requested model keeps the agent loop alive.
    if conversion_stats.get("has_image") and conversion_stats.get("tools_present"):
        chat_payload = caption_images_in_messages(chat_payload, request_model, config, request_id)
        conversion_stats["upstream_model"] = chat_payload.get("model")

    trace(
        "request.converted",
        request_id=request_id,
        stats=conversion_stats,
        upstream_model=chat_payload.get("model"),
    )
    try:
        chat, retries = call_upstream_chat(chat_payload, config, request_id)
    except ProxyError as exc:
        record_usage_event(
            model=request_model, status=int(exc.status), duration_ms=int((time.time() - started) * 1000),
            retries=exc.retries or None,
        )
        raise
    record_cache(config.cache_tracker, chat_payload.get("model"), chat.get("usage"))
    response = chat_completion_to_response(chat, request_model=request_model)
    trace(
        "response.converted",
        request_id=request_id,
        output_items=len(response.get("output", [])),
        output_text_len=len(response.get("output_text", "")),
        usage=response.get("usage"),
        cache=cache_stats_from_usage(chat.get("usage")),
    )
    inp, outp, total = _usage_tokens(chat.get("usage"))
    record_usage_event(
        model=request_model, status=200, duration_ms=int((time.time() - started) * 1000),
        input_tokens=inp, output_tokens=outp, total_tokens=total, retries=retries,
    )
    return response


def handle_streaming_request(payload: Json, config: ProxyConfig, request_id: str, wfile: Any) -> None:
    """Stream upstream response as SSE in real-time: created → text deltas → completed."""
    session_model = payload.get("model") or DEFAULT_MODEL
    payload = inject_session_model(payload, session_model)
    chat_payload, request_model, conversion_stats = responses_payload_to_chat_payload(payload)

    client_alive = True

    # Keepalive: send SSE comments every 15s while waiting for upstream first
    # byte. Prevents Codex from timing out when the model thinks for 30+
    # seconds before responding. Started before the image-caption sub-call so
    # the client never sees a silent open stream while a caption is fetched.
    keepalive_stop = threading.Event()

    def keepalive() -> None:
        while not keepalive_stop.wait(15):
            if not client_alive:
                return
            try:
                wfile.write(b": keepalive\n\n")
                wfile.flush()
            except BrokenPipeError:
                return

    ka_thread = threading.Thread(target=keepalive, daemon=True)
    ka_thread.start()

    if conversion_stats.get("has_image") and conversion_stats.get("tools_present"):
        chat_payload = caption_images_in_messages(chat_payload, request_model, config, request_id)
        conversion_stats["upstream_model"] = chat_payload.get("model")

    chat_payload["stream"] = True
    # Ask the upstream to include the usage object in the stream; without
    # stream_options.include_usage most OpenAI-compatible endpoints omit it,
    # and the cache accounting (prompt_cache_hit_tokens / cached_tokens)
    # never reaches the proxy.
    chat_payload["stream_options"] = {"include_usage": True}
    trace("request.converted", request_id=request_id, stats=conversion_stats,
          upstream_model=chat_payload.get("model"), stream=True)

    response_id = new_response_id()
    model = request_model or DEFAULT_MODEL

    def send_event(event: Json) -> None:
        nonlocal client_alive
        if not client_alive:
            return
        try:
            wfile.write(b"data: " + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n\n")
            wfile.flush()
        except BrokenPipeError:
            client_alive = False
            trace("client.disconnected", request_id=request_id, message="client closed connection during stream")

    def send_error(msg: str) -> None:
        send_event({"type": "response.error", "error": {"message": msg}})
        if client_alive:
            wfile.write(b"data: [DONE]\n\n")
            wfile.flush()

    try:
        api_key = resolve_api_key(config, request_id)
    except ProxyError as exc:
        send_error(exc.message)
        return

    send_event({"type": "response.created", "response": {
        "id": response_id, "object": "response", "created_at": now_unix(),
        "status": "in_progress", "model": model, "output": [], "output_text": "", "usage": None,
    }})

    url = f"{config.chat_base_url}/chat/completions"
    raw_payload = json.dumps(chat_payload, separators=(",",":")).encode("utf-8")
    req = urllib.request.Request(url, data=raw_payload, headers={
        "authorization": f"Bearer {api_key}", "content-type": "application/json",
        "accept": "text/event-stream",
        "user-agent": os.environ.get("OPENCODE_GO_PROXY_USER_AGENT", "codex/1.0"),
    }, method="POST")
    trace("upstream.start", request_id=request_id, url=url, bytes=len(raw_payload), stream=True)
    started = time.time()

    text = ""
    reasoning = ""
    tool_calls: list[Json] = []
    tool_call_items: dict[int, Json] = {}  # index → {id, call_id, name, namespace}
    tool_call_open: set[int] = set()  # indices already emitted as output_item.added
    usage: Json | None = None
    message_id = f"msg_{uuid.uuid4().hex}"
    reasoning_id = f"rs_{uuid.uuid4().hex}"
    item_open = False
    reasoning_open = False
    # reasoning_emitted is set the moment the reasoning item is opened and never
    # cleared: every later item's output_index is computed from it, so a
    # reasoning item that is already closed still occupies output_index 0 and
    # text/tool calls cannot collide with it.
    reasoning_emitted = False
    got_data = False

    def emit_tool_added(idx: int) -> None:
        """Emit output_item.added for tool call `idx` with its complete name.

        The upstream streams tool names in chunks (e.g. 'read_' then 'file'),
        so added must be deferred until the name is complete. The stream moves
        on to the arguments phase only after the final name chunk, so the first
        arguments delta is the signal that the name is complete; tool calls
        whose name is still growing when the stream ends are emitted in the
        finalize phase below. A truncated name must never appear in added/done.
        """
        if idx in tool_call_open:
            return
        flat_name = tool_calls[idx]["function"]["name"]
        ns, _, n = flat_name.rpartition("__")
        if not ns or not n:
            ns, n = None, flat_name
        fc_id = f"fc_{uuid.uuid4().hex}"
        call_id = tool_calls[idx]["id"] or f"call_{uuid.uuid4().hex}"
        tc_item: Json = {
            "type": "function_call", "id": fc_id, "call_id": call_id,
            "name": n, "arguments": "", "status": "in_progress",
        }
        if ns:
            tc_item["namespace"] = ns
        tool_call_items[idx] = tc_item
        send_event({"type": "response.output_item.added",
                    "output_index": (1 if reasoning_emitted else 0) + idx, "item": tc_item})
        tool_call_open.add(idx)

    # Keepalive: send SSE comments every 15s while waiting for upstream first
    # byte. Prevents Codex from timing out when the model thinks for 30+
    # seconds before responding. Started before the image-caption sub-call so
    # the client never sees a silent open stream while a caption is fetched.
    retries = 0
    max_retries = _max_retries()

    # Connect with bounded retry on transient failures before any SSE byte
    # arrives. Once the response object is open we stream it below; a failure
    # inside that loop means the upstream died after its 200 head was already
    # committed to the client, which we mark as an aborted stream rather than
    # a success.
    response = None
    while response is None:
        try:
            response = urllib.request.urlopen(req, timeout=config.timeout_sec)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            trace("upstream.error", request_id=request_id, status=exc.code, body=_mask_trace_body(body))
            if _retriable_http_status(exc.code) and retries < max_retries:
                retries += 1
                trace("upstream.retry", request_id=request_id, attempt=retries, status=exc.code)
                _retry_sleep(retries)
                continue
            keepalive_stop.set()
            if exc.code == 429:
                retry_after = exc.headers.get("retry-after", "5")
                send_error(f"rate limited (retry after {retry_after}s)")
            elif exc.code in (500, 502, 503, 504):
                send_error(f"upstream unavailable (HTTP {exc.code})")
            else:
                send_error(f"upstream HTTP {exc.code}")
            # No upstream bytes were ever streamed, so this is not a mid-stream
            # abort: meter the real final upstream status, not a synthetic 502.
            record_usage_event(model=model, status=exc.code, duration_ms=int((time.time() - started) * 1000),
                               retries=retries)
            return
        except (urllib.error.URLError, TimeoutError) as exc:
            trace("upstream.network_error", request_id=request_id, reason=str(getattr(exc, "reason", exc)))
            if retries < max_retries:
                retries += 1
                trace("upstream.retry", request_id=request_id, attempt=retries, reason=str(getattr(exc, "reason", exc)))
                _retry_sleep(retries)
                continue
            keepalive_stop.set()
            send_error(f"upstream network error: {getattr(exc, 'reason', exc)}")
            # Network failure: no upstream status exists (502) and nothing was
            # streamed, so no streamAborted marker.
            record_usage_event(model=model, status=502, duration_ms=int((time.time() - started) * 1000),
                               retries=retries)
            return

    try:
        with response as resp:
            keepalive_stop.set()  # Stop keepalive once upstream starts responding.
            for line in resp:
                line = line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                got_data = True
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                # Reasoning — stream summary deltas so Codex shows thinking text in real-time.
                r = delta.get("reasoning_content")
                if isinstance(r, str) and r:
                    if not reasoning_open:
                        send_event({"type": "response.output_item.added", "output_index": 0, "item": {
                            "type": "reasoning", "id": reasoning_id, "summary": [], "status": "in_progress",
                        }})
                        reasoning_open = True
                        reasoning_emitted = True
                    reasoning += r
                    send_event({"type": "response.reasoning_summary_text.delta",
                                "item_id": reasoning_id, "output_index": 0, "summary_index": 0, "delta": r})
                # Text delta — open item lazily, then stream.
                d = delta.get("content")
                if isinstance(d, str) and d:
                    if not item_open:
                        idx = 1 if reasoning_emitted else 0
                        send_event({"type": "response.output_item.added", "output_index": idx, "item": {
                            "type": "message", "id": message_id, "role": "assistant",
                            "status": "in_progress", "content": [],
                        }})
                        item_open = True
                    text += d
                    send_event({"type": "response.output_text.delta", "item_id": message_id, "output_index": 1 if reasoning_emitted else 0, "delta": d})
                tcs = delta.get("tool_calls")
                if isinstance(tcs, list) and tcs and reasoning_open:
                    # Close reasoning item before tool calls so UI shows tool calls, not "thinking".
                    rs_done = {"type": "reasoning", "id": reasoning_id,
                               "summary": [{"type": "summary_text", "text": reasoning}], "status": "completed"}
                    send_event({"type": "response.output_item.done", "output_index": 0, "item": rs_done})
                    reasoning_open = False
                if isinstance(tcs, list):
                    for tc in tcs:
                        idx = tc.get("index", 0)
                        while len(tool_calls) <= idx:
                            tool_calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            tool_calls[idx]["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        name_delta = fn.get("name")
                        if name_delta:
                            tool_calls[idx]["function"]["name"] += name_delta
                        args_delta = fn.get("arguments")
                        if args_delta:
                            tool_calls[idx]["function"]["arguments"] += args_delta
                            # Arguments only start after the final name chunk, so
                            # the accumulated name is complete: emit added now.
                            emit_tool_added(idx)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        # The upstream stream died after its 200 head was already committed to
        # the client. Surface an error and mark the turn as aborted so the
        # meter never records a success the client never received.
        keepalive_stop.set()
        code = exc.code if isinstance(exc, urllib.error.HTTPError) else None
        trace("upstream.stream_aborted", request_id=request_id,
              status=code or getattr(exc, "reason", str(exc)))
        send_error("upstream stream aborted")
        record_usage_event(model=model, status=502, duration_ms=int((time.time() - started) * 1000),
                           stream_aborted=True, retries=retries)
        return

    duration_ms = int((time.time() - started) * 1000)
    trace("upstream.done", request_id=request_id, status=200, elapsed_ms=duration_ms, stream=True)
    record_cache(config.cache_tracker, chat_payload.get("model"), usage)

    if not client_alive:
        trace("client.gone", request_id=request_id, message="client disconnected before final events")
        record_usage_event(model=model, status=0, duration_ms=duration_ms, stream_aborted=True, retries=retries)
        return

    if not got_data:
        send_error("upstream returned no SSE data")
        record_usage_event(model=model, status=502, duration_ms=duration_ms, empty_completion=True, retries=retries)
        return

    # Build final response from accumulated data.
    fake_msg: Json = {}
    if reasoning:
        fake_msg["reasoning_content"] = reasoning
    if tool_calls:
        fake_msg["tool_calls"] = tool_calls
    if text:
        fake_msg["content"] = text
    output = chat_message_to_response_output(fake_msg)

    # Close reasoning item if opened.
    if reasoning_open:
        rs_done = {"type": "reasoning", "id": reasoning_id,
                    "summary": [{"type": "summary_text", "text": reasoning}], "status": "completed"}
        send_event({"type": "response.output_item.done", "output_index": 0, "item": rs_done})

    # Emit output_item.done for tool calls that were opened during streaming,
    # and added+done for any that weren't (e.g. name only completed at stream
    # end) — always with the complete name.
    tc_base = 1 if reasoning_emitted else 0
    tc_count = 0
    for item in output:
        if item.get("type") != "function_call":
            continue
        idx = tc_base + tc_count
        if tc_count in tool_call_open:
            # Already emitted added during streaming; close with the final
            # name/arguments and keep the streamed item id.
            done_item = dict(tool_call_items[tc_count])
            done_item["name"] = item["name"]
            done_item["arguments"] = item.get("arguments", "{}")
            done_item["status"] = "completed"
            send_event({"type": "response.output_item.done", "output_index": idx, "item": done_item})
            # Completed must reuse the streamed ids so Codex reconciles items
            # by id instead of creating ghost duplicates.
            item["id"] = tool_call_items[tc_count]["id"]
            item["call_id"] = tool_call_items[tc_count]["call_id"]
        else:
            # Added was never emitted during streaming: emit added+done now
            # with the complete name.
            tool_call_items[tc_count] = item
            send_event({"type": "response.output_item.added", "output_index": idx, "item": item})
            send_event({"type": "response.output_item.done", "output_index": idx, "item": item})
        tc_count += 1

    # Close message item if opened.
    if item_open:
        msg_idx = tc_base + len(tool_calls)
        msg_done = {"type": "message", "id": message_id, "role": "assistant", "status": "completed",
                     "content": [{"type": "output_text", "text": text, "annotations": []}]}
        send_event({"type": "response.output_item.done", "output_index": msg_idx, "item": msg_done})

    # Reuse the ids already streamed in response.completed.
    for out_item in output:
        if out_item.get("type") == "reasoning":
            out_item["id"] = reasoning_id
        elif out_item.get("type") == "message":
            out_item["id"] = message_id

    final: Json = {
        "id": response_id, "object": "response", "created_at": now_unix(),
        "status": "completed", "model": model, "output": output,
        "output_text": text, "usage": normalize_usage(usage),
    }
    send_event({"type": "response.completed", "response": final})
    wfile.write(b"data: [DONE]\n\n")
    wfile.flush()
    trace("response.converted", request_id=request_id, output_items=len(output),
          output_text_len=len(text), usage=final.get("usage"), stream=True,
          cache=cache_stats_from_usage(usage))
    inp, outp, total = _usage_tokens(usage)
    empty = not text and not tool_calls and not reasoning
    record_usage_event(model=model, status=200, duration_ms=duration_ms,
                       input_tokens=inp, output_tokens=outp, total_tokens=total,
                       retries=retries, empty_completion=empty)


def caption_images_in_messages(chat_payload: Json, target_model: str, config: ProxyConfig, request_id: str) -> Json:
    """Replace image_url parts with vision-generated text captions. Routes turn to target_model after."""
    image_model = vision.resolve_caption_model(target_model)
    messages = chat_payload.get("messages", [])

    # Collect all image URLs across messages.
    image_jobs: list[tuple[int, int, str]] = []  # (msg_idx, part_idx, url)
    for mi, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for pi, part in enumerate(content):
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url:
                    image_jobs.append((mi, pi, url))

    if not image_jobs:
        chat_payload["model"] = target_model
        return chat_payload

    # Only caption the latest image; stub older ones to save 25+ seconds per turn.
    # Old screenshots are stale context — the model only needs the current screen to act.
    latest = image_jobs[-1]
    caption = caption_image(latest[2], image_model, config, request_id)
    for mi, pi, _url in image_jobs[:-1]:
        messages[mi]["content"][pi] = {"type": "text", "text": "[prior screenshot omitted]"}
    mi, pi, _ = latest
    messages[mi]["content"][pi] = {"type": "text", "text": f"[screenshot: {caption}]"}

    # Collapse text-only lists back to strings (fast path for upstream).
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and all(
            isinstance(p, dict) and p.get("type") == "text" for p in content
        ):
            message["content"] = "\n".join(p.get("text", "") for p in content if p.get("text"))

    chat_payload["model"] = target_model
    trace("split_turn.captioned", request_id=request_id, captions=1, omitted=len(image_jobs) - 1, model=chat_payload["model"])
    return chat_payload


# Only these 4xx statuses mean "this model cannot read the image" or "this
# detail value is unsupported". Auth failures (401/403) and rate limits (429)
# degrade immediately so a bad key never fans out into three caption calls.
_CAPTION_FALLBACK_STATUSES = {400, 404, 415, 422}


def _caption_rejection_status(exc: ProxyError) -> int:
    return exc.upstream_status if exc.upstream_status is not None else int(exc.status)


def _caption_attempt(image_url: str, image_model: str, detail: str | None, config: ProxyConfig, request_id: str) -> tuple[Json | None, ProxyError | None]:
    """One caption sub-call with no transient retries; returns (chat, error)."""
    payload = vision.build_caption_payload(image_url, image_model, detail=detail)
    try:
        chat, _retries = call_upstream_chat(payload, config, request_id, timeout_sec=_caption_timeout_sec(), max_retries=0)
        return chat, None
    except ProxyError as exc:
        return None, exc


def caption_image(image_url: str, image_model: str, config: ProxyConfig, request_id: str) -> str:
    """Caption one image via a vision-capable model, served from a byte-keyed cache.

    Returns a text description. A failed caption degrades to a placeholder;
    it never blocks the turn.
    """
    image_bytes = vision.image_bytes_for_cache(image_url)
    cached = vision.CAPTION_CACHE.get(image_bytes)
    if cached is not None:
        trace("split_turn.caption_cache_hit", request_id=request_id)
        return cached

    detail = vision.caption_detail()
    url, model = image_url, image_model
    chat, exc = _caption_attempt(url, model, detail, config, request_id)
    if exc is not None and _caption_rejection_status(exc) in _CAPTION_FALLBACK_STATUSES and model != IMAGE_MODEL_DEFAULT:
        # The turn model may reject image input; fall back to the known
        # vision model for this one sub-call.
        model = IMAGE_MODEL_DEFAULT
        trace("split_turn.caption_fallback", request_id=request_id, kind="engine", model=model, status=_caption_rejection_status(exc))
        chat, exc = _caption_attempt(url, model, detail, config, request_id)
    if exc is not None and _caption_rejection_status(exc) in _CAPTION_FALLBACK_STATUSES and detail is not None:
        # Unknown detail values can 4xx on some upstreams; retry with a
        # downscaled image (or the original URL) and no detail.
        downscaled = vision.downscale_data_url(url)
        if downscaled is not None:
            url = downscaled
        detail = None
        trace("split_turn.caption_fallback", request_id=request_id, kind="detail", status=_caption_rejection_status(exc))
        chat, exc = _caption_attempt(url, model, detail, config, request_id)
    if exc is not None:
        trace("split_turn.caption_failed", request_id=request_id, status=exc.status, message=exc.message[:200])
        return f"[caption failed: {exc.message[:100]}]"

    record_cache(config.cache_tracker, model, chat.get("usage"))
    choice = (chat.get("choices") or [{}])[0]
    text = (choice.get("message", {}) or {}).get("content", "")
    caption = text.strip() if isinstance(text, str) and text.strip() else "[caption unavailable]"
    vision.CAPTION_CACHE.put(image_bytes, caption)
    return caption



def call_upstream_chat(chat_payload: Json, config: ProxyConfig, request_id: str, *, timeout_sec: float | None = None, max_retries: int | None = None) -> tuple[Json, int]:
    api_key = resolve_api_key(config, request_id)

    url = f"{config.chat_base_url}/chat/completions"
    raw_payload = json.dumps(chat_payload, separators=(",",":")).encode("utf-8")
    max_retries = _max_retries() if max_retries is None else max_retries
    retries = 0
    while True:
        request = urllib.request.Request(
            url,
            data=raw_payload,
            headers={
                "authorization": f"Bearer {api_key}",
                "content-type": "application/json",
                "accept": "application/json",
                "user-agent": os.environ.get("OPENCODE_GO_PROXY_USER_AGENT", "codex/1.0"),
            },
            method="POST",
        )
        trace("upstream.start", request_id=request_id, url=url, bytes=len(raw_payload), attempt=retries + 1)
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=timeout_sec or config.timeout_sec) as response:
                body = response.read()
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
            if _retriable_http_status(exc.code) and retries < max_retries:
                retries += 1
                trace("upstream.retry", request_id=request_id, attempt=retries, status=exc.code)
                _retry_sleep(retries)
                continue
            if exc.code == 429:
                retry_after = exc.headers.get("retry-after", "5")
                raise ProxyError(HTTPStatus.TOO_MANY_REQUESTS, f"rate limited (retry after {retry_after}s)", retries=retries) from exc
            if exc.code == 503:
                raise ProxyError(HTTPStatus.SERVICE_UNAVAILABLE, "upstream unavailable", retries=retries) from exc
            if exc.code == 504:
                raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "upstream timeout", retries=retries) from exc
            raise ProxyError(HTTPStatus.BAD_GATEWAY, f"upstream HTTP {exc.code}", retries=retries, upstream_status=exc.code) from exc
        except urllib.error.URLError as exc:
            trace("upstream.network_error", request_id=request_id, reason=str(getattr(exc, "reason", exc)))
            if retries < max_retries:
                retries += 1
                trace("upstream.retry", request_id=request_id, attempt=retries, reason=str(getattr(exc, "reason", exc)))
                _retry_sleep(retries)
                continue
            raise ProxyError(HTTPStatus.BAD_GATEWAY, f"upstream network error: {getattr(exc, 'reason', exc)}", retries=retries) from exc
        except TimeoutError:
            trace("upstream.timeout", request_id=request_id, timeout=timeout_sec or config.timeout_sec)
            if retries < max_retries:
                retries += 1
                trace("upstream.retry", request_id=request_id, attempt=retries, reason="timeout")
                _retry_sleep(retries)
                continue
            raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "upstream timeout", retries=retries) from None


_api_key_cache: str | None = None
_api_key_lock = threading.Lock()

# Keychain services that may hold the OpenCode Go API key. The proxy's own
# install uses opencode-go-api-key; the codex-router install on the same
# machine stores it under codex-router-opencode-go. Trying both keeps the
# proxy working regardless of which harness provisioned the credential.
_KEYCHAIN_SERVICES: tuple[str, ...] = ("opencode-go-api-key", "codex-router-opencode-go")


def resolve_api_key(config: ProxyConfig, request_id: str) -> str:
    global _api_key_cache
    if _api_key_cache:
        return _api_key_cache

    with _api_key_lock:
        if _api_key_cache:
            return _api_key_cache

        # OPENCODE_API_KEY is the standard OpenCode env var; accept it as a
        # fallback so the proxy works in environments that provisioned the
        # key under the generic name.
        for env in (config.api_key_env, "OPENCODE_API_KEY"):
            if not env:
                continue
            api_key = os.environ.get(env)
            if api_key:
                _api_key_cache = api_key
                trace("credential.source", request_id=request_id, source="env", env=env)
                return api_key

        services: list[str] = []
        service_env = os.environ.get("CODEX_KEYCHAIN_SERVICE")
        if service_env:
            services.append(service_env)
        services.extend(_KEYCHAIN_SERVICES)
        for keychain_service in dict.fromkeys(services):
            trace("credential.lookup", request_id=request_id, source="keychain", service=keychain_service)
            try:
                completed = subprocess.run(
                    ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", keychain_service, "-w"],
                    check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                completed = None
            if completed and completed.returncode == 0:
                first_line = completed.stdout.splitlines()[0].strip() if completed.stdout.splitlines() else ""
                if first_line:
                    _api_key_cache = first_line
                    trace("credential.source", request_id=request_id, source="keychain", service=keychain_service)
                    return first_line

        raise ProxyError(
            HTTPStatus.UNAUTHORIZED,
            f"missing API key: set ${config.api_key_env} or $OPENCODE_API_KEY or keychain:{_KEYCHAIN_SERVICES[0]}",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex Responses API shim for OpenAI Chat Completions upstreams")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--bind", default=os.environ.get("OPENCODE_GO_PROXY_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("OPENCODE_GO_PROXY_PORT", "8787")))
    parser.add_argument(
        "--chat-base-url",
        dest="chat_base_url",
        default=os.environ.get("CHAT_COMPLETIONS_BASE_URL", "https://opencode.ai/zen/go/v1"),
    )
    parser.add_argument("--api-key-env", default=os.environ.get("OPENCODE_GO_PROXY_API_KEY_ENV", "OPENCODE_GO_API_KEY"))
    parser.add_argument("--timeout-sec", type=float, default=float(os.environ.get("OPENCODE_GO_PROXY_TIMEOUT_SEC", "180")))
    parser.add_argument("--max-body-mb", type=int, default=int(os.environ.get("OPENCODE_GO_PROXY_MAX_BODY_MB", "20")))
    return parser


def main(argv: list[str] | None = None) -> None:
    args_list = list(sys.argv[1:] if argv is None else argv)
    # Operational subcommands must be reachable from the installed console
    # script (entry point is app:main), not only via python -m. Dispatch them
    # before any server startup.
    if "--refresh-catalog" in args_list or os.environ.get("OPENCODE_GO_PROXY_REFRESH_CATALOG") == "1":
        args_list = [a for a in args_list if a != "--refresh-catalog"]
        from . import catalog

        sys.exit(catalog.main_refresh(args_list))
    if args_list and args_list[0] == "doctor":
        from . import ops

        sys.exit(ops.doctor(args_list[1:]))
    if args_list and args_list[0] == "smoke-test":
        from . import ops

        sys.exit(ops.smoke_test())
    if args_list and args_list[0] == "support-bundle":
        from . import ops

        sys.exit(ops.support_bundle(args_list[1:]))
    args = build_parser().parse_args(args_list)
    try:
        from opencode_go_proxy import catalog as _catalog

        _catalog.refresh_catalog()
    except Exception as exc:  # noqa: BLE001 - startup refresh is best-effort
        trace("catalog.refresh.skipped", error=str(exc))
    config = ProxyConfig(
        bind=args.bind,
        port=args.port,
        chat_base_url=args.chat_base_url,
        api_key_env=args.api_key_env,
        timeout_sec=args.timeout_sec,
        max_body_bytes=args.max_body_mb * 1024 * 1024,
    )
    if config.bind not in {"127.0.0.1", "localhost", "::1"}:
        trace("security.warning", bind=config.bind,
              message="binding to non-localhost address — proxy exposes upstream API key to network")
    server = ThreadingHTTPServer((config.bind, config.port), ResponsesProxyHandler)
    server.config = config  # type: ignore[attr-defined]
    trace(
        "server.start",
        bind=config.bind,
        port=config.port,
        chat_base_url=config.chat_base_url,
        api_key_env=config.api_key_env,
    )
    # serve_forever in a background thread: shutdown() from a signal handler
    # running on the main thread would otherwise deadlock (both need the main
    # thread), leaving the process unkillable via SIGTERM.
    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    signal.signal(signal.SIGTERM, lambda *_: server.shutdown())
    try:
        serve_thread.start()
        serve_thread.join()
    except KeyboardInterrupt:
        trace("server.stop", reason="keyboard_interrupt")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
