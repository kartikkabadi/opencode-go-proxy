"""Thin HTTP dispatch for the Responses-API shim.

Route handling and request plumbing live here; secrets, the upstream client,
the SSE streaming engine, and vision captions live in their own modules.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import signal
import sys
import threading
import time
import traceback
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__
from .config import ProxyConfig, resolve_chat_base_url
from .errors import ProxyError
from .guards import check_browser_origin, check_content_type, check_host
from .meter import (
    DEFAULT_ESTIMATE_CONTEXT_WINDOW,
    estimate_input_tokens,
    record_usage_event,
)
from .passthrough import relay_native_request
from .protocol import (
    DEFAULT_MODEL,
    cache_stats_from_usage,
    chat_completion_to_response,
    inject_session_model,
    known_models,
    model_context_window,
    responses_payload_to_chat_payload,
)
from .quota import read_quota_state
from .routing import route_target
from .state import build_state
from .streaming import handle_chat_stream_passthrough, handle_streaming_request
from .trace import trace
from .upstream import (
    call_upstream_chat,
    call_upstream_chat_verbatim,
    record_cache,
    usage_tokens,
)
from .vision import caption_images_in_messages, is_image_rejection_status

Json = dict[str, Any]

RESPONSES_PATHS = {"/responses", "/v1/responses", "/responses/compact", "/v1/responses/compact"}
CHAT_COMPLETIONS_PATHS = {"/chat/completions", "/v1/chat/completions"}
MESSAGES_PATHS = {"/messages", "/v1/messages"}
MESSAGES_UNSUPPORTED: Json = {
    "error": {
        "type": "invalid_request_error",
        "message": (
            "This proxy serves a single OpenAI-compatible provider via "
            "/v1/chat/completions and /v1/responses; /messages is not supported."
        ),
    }
}


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
    def _guard_request(self) -> None:
        """Plan 006 transport guard: loopback Host, then no browser markers."""
        check_host(self.headers.get("Host"))
        check_browser_origin(self.headers)

    @staticmethod
    def _error_payload(exc: ProxyError) -> Json:
        return {"error": {"type": exc.error_type or "proxy_error", "message": exc.message}}

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
        # A realtime WebSocket upgrade must answer 426 before any auth guard:
        # upgrades carry browser markers (Origin) that would otherwise 403.
        if self._reject_websocket_upgrade():
            return
        try:
            self._guard_request()
        except ProxyError as exc:
            trace("request.failed", status=exc.status, message=exc.message)
            self._send_json(self._error_payload(exc), status=exc.status)
            return
        if self.path in {"/health", "/v1/health"}:
            self._send_json({"status": "ok"})
            return
        if self.path in {"/quota", "/v1/quota"}:
            self._send_json(read_quota_state())
            return
        if self.path in {"/state", "/v1/state"}:
            config = self._config()
            self._send_json(build_state(port=config.port, upstream=config.chat_base_url))
            return
        if self.path in {"/cache", "/v1/cache", "/metrics", "/v1/metrics"}:
            config = self._config()
            self._send_json(config.cache_tracker.snapshot())
            return
        if self.path in {"/models", "/v1/models"}:
            from opencode_go_proxy import catalog as _catalog

            slugs = _catalog.merged_model_slugs() or sorted(known_models())
            self._send_json({
                "object": "list",
                "data": [{"id": slug, "object": "model"} for slug in slugs],
            })
            return
        if self.path in MESSAGES_PATHS:
            self._send_json(MESSAGES_UNSUPPORTED, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"error": {"message": "not found"}}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        request_id = uuid.uuid4().hex[:12]
        # The Codex app may send ?compact=true on the responses path; the query
        # string must not turn a known path into a 404. The payload translates
        # through the normal path either way (compact is just a hint).
        path = self.path.split("?", 1)[0]

        try:
            self._guard_request()
            if path not in RESPONSES_PATHS | CHAT_COMPLETIONS_PATHS:
                if path in MESSAGES_PATHS:
                    self._send_json(MESSAGES_UNSUPPORTED, status=HTTPStatus.BAD_REQUEST)
                else:
                    self._send_json({"error": {"message": "not found"}}, status=HTTPStatus.NOT_FOUND)
                return
            check_content_type(self.headers.get("content-type"))
            config = self._config()
            payload = self._read_json(config)
            trace(
                "request.received",
                request_id=request_id,
                path=self.path,
                model=payload.get("model"),
                stream=payload.get("stream", False),
            )
            if path in CHAT_COMPLETIONS_PATHS:
                handle_chat_completions_request(self, payload, config, request_id)
            elif path in RESPONSES_PATHS and route_target(payload.get("model") or DEFAULT_MODEL) == "native":
                # Native models relay whole to the ChatGPT backend: the body,
                # the client's own auth, and the stream are forwarded verbatim
                # and never touch the OpenCode Go key or alias logic.
                relay_native_request(self, payload, config, request_id)
            elif payload.get("stream") is True:
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
            self._send_json(self._error_payload(exc), status=exc.status)
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

    def _config(self) -> ProxyConfig:
        return self.server.config  # type: ignore[attr-defined]

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
    fell_back = False
    try:
        chat, retries = call_upstream_chat(chat_payload, config, request_id)
    except ProxyError as exc:
        status = exc.upstream_status if exc.upstream_status is not None else int(exc.status)
        if (
            conversion_stats.get("has_image")
            and not conversion_stats.get("tools_present")
            and is_image_rejection_status(status)
        ):
            # Runtime rescue (plan 009 escape hatch): the catalog promised the
            # requested model image input but the upstream rejected the payload.
            # Caption the images and retry the same requested model once.
            fell_back = True
            trace("image_fallback", request_id=request_id, kind="caption", status=status, model=request_model)
            chat_payload = caption_images_in_messages(chat_payload, request_model, config, request_id)
            conversion_stats["upstream_model"] = chat_payload.get("model")
            try:
                chat, retries = call_upstream_chat(chat_payload, config, request_id)
            except ProxyError as exc2:
                record_usage_event(
                    model=request_model, status=int(exc2.status), duration_ms=int((time.time() - started) * 1000),
                    retries=(exc2.retries or 0) + 1,
                )
                raise
        else:
            record_usage_event(
                model=request_model, status=int(exc.status), duration_ms=int((time.time() - started) * 1000),
                retries=exc.retries or None,
            )
            raise
    record_cache(config.cache_tracker, chat_payload.get("model"), chat.get("usage"))
    context_cap = model_context_window(request_model) or DEFAULT_ESTIMATE_CONTEXT_WINDOW
    prompt_bytes = len(json.dumps(chat_payload, separators=(",", ":")).encode("utf-8"))
    estimated = estimate_input_tokens(request_model, prompt_bytes, chat.get("usage"), context_window=context_cap)
    response = chat_completion_to_response(chat, request_model=request_model, estimated_input_tokens=estimated)
    trace(
        "response.converted",
        request_id=request_id,
        output_items=len(response.get("output", [])),
        output_text_len=len(response.get("output_text", "")),
        usage=response.get("usage"),
        cache=cache_stats_from_usage(chat.get("usage")),
    )
    inp, outp, total = usage_tokens(chat.get("usage"))
    record_usage_event(
        model=request_model, status=200, duration_ms=int((time.time() - started) * 1000),
        input_tokens=inp, output_tokens=outp, total_tokens=total,
        estimated_input_tokens=estimated,
        retries=retries + (1 if fell_back else 0),
    )
    return response


def handle_chat_completions_request(handler: ResponsesProxyHandler, payload: Json, config: ProxyConfig, request_id: str) -> None:
    """Verbatim /chat/completions passthrough for both stream modes.

    Non-stream relays the upstream status and JSON body verbatim, including the
    upstream's own error body on 4xx/5xx (never a ``proxy_error`` envelope).
    Stream commits SSE only after the upstream answers 200 and relays its bytes
    unchanged. A missing key surfaces the same 401 proxy error the responses
    path uses.
    """
    if payload.get("stream") is True:
        handle_chat_stream_passthrough(payload, config, request_id, handler)
        return
    started = time.time()
    status, body, retries, content_type = call_upstream_chat_verbatim(payload, config, request_id)
    record_usage_event(
        model=payload.get("model") or DEFAULT_MODEL, status=status,
        duration_ms=int((time.time() - started) * 1000), retries=retries or None,
    )
    handler.send_response(status)
    handler.send_header("content-type", content_type or "application/json")
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex Responses API shim for OpenAI Chat Completions upstreams")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--bind", default=os.environ.get("OPENCODE_GO_PROXY_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("OPENCODE_GO_PROXY_PORT", "8787")))
    parser.add_argument(
        "--chat-base-url",
        dest="chat_base_url",
        default=resolve_chat_base_url(),
    )
    parser.add_argument("--api-key-env", default=os.environ.get("OPENCODE_GO_PROXY_API_KEY_ENV", "OPENCODE_GO_API_KEY"))
    parser.add_argument("--timeout-sec", type=float, default=float(os.environ.get("OPENCODE_GO_PROXY_TIMEOUT_SEC", "180")))
    parser.add_argument("--max-body-mb", type=int, default=int(os.environ.get("OPENCODE_GO_PROXY_MAX_BODY_MB", "20")))
    return parser


def _refresh_catalog_in_background() -> None:
    """Run the TTL-gated runtime catalog refresh; failures are logged, never fatal."""
    try:
        from opencode_go_proxy import catalog as _catalog

        _catalog.refresh_runtime_catalog()
        _catalog.render_merged_catalog()
    except Exception as exc:  # noqa: BLE001 - background refresh is best-effort
        trace("catalog.refresh.skipped", error=str(exc))


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

        sys.exit(ops.smoke_test(args_list[1:]))
    if args_list and args_list[0] in {"setup", "install"}:
        from . import ops

        sys.exit(ops.install(args_list[1:]))
    if args_list and args_list[0] == "install-skills":
        from . import ops

        sys.exit(ops.install_skills(args_list[1:]))
    if args_list and args_list[0] == "agents-sync":
        from . import agents_sync

        sys.exit(agents_sync.agents_sync_cmd(args_list[1:]))
    if args_list and args_list[0] == "status":
        from . import ops

        sys.exit(ops.status(args_list[1:]))
    if args_list and args_list[0] == "support-bundle":
        from . import ops

        sys.exit(ops.support_bundle(args_list[1:]))
    if args_list and args_list[0] == "config":
        from . import config_manager

        sys.exit(config_manager.config_cmd(args_list[1:]))
    if args_list and args_list[0] == "models":
        from . import models_cmd

        sys.exit(models_cmd.models_cmd(args_list[1:]))
    if args_list and args_list[0] == "native-capture":
        from . import native_models

        sys.exit(native_models.native_capture_cmd(args_list[1:]))
    if args_list and args_list[0] == "refresh-runtime":
        from . import catalog, native_models

        # The menu bar's Refresh Catalog: re-capture the native set, then
        # force a runtime render under the state dir (the catalog the proxy
        # actually serves and the config block points Codex at).
        try:
            native_models.capture_native_models()
        except native_models.NativeCaptureError as exc:
            print(f"warning: native capture skipped: {exc}")
        rendered = catalog.refresh_runtime_catalog(force=True)
        print(f"runtime catalog refreshed: {len(rendered.get('models', []))} models")
        catalog.render_merged_catalog()
        sys.exit(0)
    args = build_parser().parse_args(args_list)
    try:
        from opencode_go_proxy import catalog as _catalog
        from opencode_go_proxy import native_models

        # Startup fast path: render the state-dir compact (or the seed) with
        # no network, capture the native catalog so a fresh install serves
        # official GPT models immediately (best-effort: a missing or logged-
        # out codex binary keeps the last snapshot), then render the merged
        # native + opencode-go catalog. The full refresh below runs in the
        # background.
        _catalog.prepare_runtime_catalog()
        native_models.capture_native_models()
        _catalog.render_merged_catalog()
    except Exception as exc:  # noqa: BLE001 - startup catalog render is best-effort
        trace("catalog.refresh.skipped", error=str(exc))
    # The full refresh may fetch models.dev (up to a 10s timeout); run it in
    # the background so startup never blocks on the network.
    threading.Thread(target=_refresh_catalog_in_background, daemon=True, name="catalog-refresh").start()
    if args.timeout_sec <= 0:
        sys.stderr.write(f"error: --timeout-sec must be positive, got {args.timeout_sec}\n")
        sys.exit(2)
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
