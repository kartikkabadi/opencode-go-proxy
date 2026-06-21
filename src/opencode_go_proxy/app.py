from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .protocol import (
    DEFAULT_MODEL,
    IMAGE_MODEL_DEFAULT,
    KNOWN_MODELS,
    chat_completion_to_response,
    chat_message_to_response_output,
    new_response_id,
    normalize_usage,
    now_unix,
    responses_payload_to_chat_payload,
)


Json = dict[str, Any]


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


def trace(event: str, **fields: Any) -> None:
    record = {"ts": time.time(), "event": event, **fields}
    print(json.dumps(record, sort_keys=True), file=sys.stderr, flush=True)


class DeepSeekResponsesHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in {"/health", "/v1/health"}:
            self._send_json({"status": "ok"})
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
                handle_streaming_request(payload, config, request_id, self.wfile)
            else:
                response = handle_responses_request(payload, config, request_id)
                self._send_json(response)
        except ProxyError as exc:
            trace("request.failed", request_id=request_id, status=exc.status, message=exc.message)
            self._send_json({"error": {"message": exc.message, "type": "proxy_error"}}, status=exc.status)
        except BrokenPipeError:
            trace("client.disconnected", request_id=request_id, message="client closed connection during stream")
        except Exception as exc:  # pragma: no cover - defensive crash trace
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
        value = json.loads(raw)
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
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def handle_responses_request(payload: Json, config: ProxyConfig, request_id: str) -> Json:
    chat_payload, request_model, conversion_stats = responses_payload_to_chat_payload(payload)

    # Split-turn: if image + tools, caption images via MiMo sub-call, then route to DeepSeek.
    # MiMo can't drive tool loops from tool-role image messages; caption + DeepSeek keeps the agent loop alive.
    if conversion_stats.get("has_image") and conversion_stats.get("tools_present"):
        chat_payload = caption_images_in_messages(chat_payload, request_model, config, request_id)
        conversion_stats["upstream_model"] = chat_payload.get("model")

    trace(
        "request.converted",
        request_id=request_id,
        stats=conversion_stats,
        upstream_model=chat_payload.get("model"),
    )
    chat = call_upstream_chat(chat_payload, config, request_id)
    response = chat_completion_to_response(chat, request_model=request_model)
    trace(
        "response.converted",
        request_id=request_id,
        output_items=len(response.get("output", [])),
        output_text_len=len(response.get("output_text", "")),
        usage=response.get("usage"),
    )
    return response


def handle_streaming_request(payload: Json, config: ProxyConfig, request_id: str, wfile: Any) -> None:
    """Stream upstream response as SSE in real-time: created → text deltas → completed."""
    chat_payload, request_model, conversion_stats = responses_payload_to_chat_payload(payload)

    if conversion_stats.get("has_image") and conversion_stats.get("tools_present"):
        chat_payload = caption_images_in_messages(chat_payload, request_model, config, request_id)
        conversion_stats["upstream_model"] = chat_payload.get("model")

    chat_payload["stream"] = True
    trace("request.converted", request_id=request_id, stats=conversion_stats,
          upstream_model=chat_payload.get("model"), stream=True)

    response_id = new_response_id()
    model = request_model or DEFAULT_MODEL

    def send_event(event: Json) -> None:
        wfile.write(b"data: " + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n\n")
        wfile.flush()

    def send_error(msg: str) -> None:
        send_event({"type": "response.error", "error": {"message": msg}})
        wfile.write(b"data: [DONE]\n\n")
        wfile.flush()

    send_event({"type": "response.created", "response": {
        "id": response_id, "object": "response", "created_at": now_unix(),
        "status": "in_progress", "model": model, "output": [], "output_text": "", "usage": None,
    }})

    api_key = resolve_api_key(config, request_id)
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
    usage: Json | None = None
    message_id = f"msg_{uuid.uuid4().hex}"
    reasoning_id = f"rs_{uuid.uuid4().hex}"
    item_open = False
    reasoning_open = False
    got_data = False

    try:
        with urllib.request.urlopen(req, timeout=config.timeout_sec) as resp:
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
                # Reasoning — open item so Codex shows thinking indicator, but don't stream deltas
                # (response.reasoning.delta is non-standard; Codex ignores it). Accumulate for final response.
                r = delta.get("reasoning_content")
                if isinstance(r, str) and r:
                    if not reasoning_open:
                        send_event({"type": "response.output_item.added", "output_index": 0, "item": {
                            "type": "reasoning", "id": reasoning_id, "summary": [], "status": "in_progress",
                        }})
                        reasoning_open = True
                    reasoning += r
                # Text delta — open item lazily, then stream.
                d = delta.get("content")
                if isinstance(d, str) and d:
                    if not item_open:
                        idx = 1 if reasoning_open else 0
                        send_event({"type": "response.output_item.added", "output_index": idx, "item": {
                            "type": "message", "id": message_id, "role": "assistant",
                            "status": "in_progress", "content": [],
                        }})
                        item_open = True
                    text += d
                    send_event({"type": "response.output_text.delta", "item_id": message_id, "output_index": 1 if reasoning_open else 0, "delta": d})
                tcs = delta.get("tool_calls")
                if isinstance(tcs, list):
                    for tc in tcs:
                        idx = tc.get("index", 0)
                        while len(tool_calls) <= idx:
                            tool_calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            tool_calls[idx]["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            tool_calls[idx]["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            tool_calls[idx]["function"]["arguments"] += fn["arguments"]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        trace("upstream.error", request_id=request_id, status=exc.code, body=body[:2000])
        if exc.code == 429:
            retry_after = exc.headers.get("retry-after", "5")
            send_error(f"rate limited (retry after {retry_after}s)")
        elif exc.code in (500, 502, 503, 504):
            send_error(f"upstream unavailable (HTTP {exc.code})")
        else:
            send_error(f"upstream HTTP {exc.code}")
        return
    except (urllib.error.URLError, TimeoutError) as exc:
        trace("upstream.network_error", request_id=request_id, reason=str(getattr(exc, "reason", exc)))
        send_error(f"upstream network error: {getattr(exc, 'reason', exc)}")
        return

    trace("upstream.done", request_id=request_id, status=200,
          elapsed_ms=int((time.time() - started) * 1000), stream=True)

    if not got_data:
        send_error("upstream returned no SSE data")
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

    # Close message item if opened.
    if item_open:
        msg_idx = 1 if reasoning_open else 0
        msg_done = {"type": "message", "id": message_id, "role": "assistant", "status": "completed",
                     "content": [{"type": "output_text", "text": text, "annotations": []}]}
        send_event({"type": "response.output_item.done", "output_index": msg_idx, "item": msg_done})

    final: Json = {
        "id": response_id, "object": "response", "created_at": now_unix(),
        "status": "completed", "model": model, "output": output,
        "output_text": text, "usage": normalize_usage(usage),
    }
    send_event({"type": "response.completed", "response": final})
    wfile.write(b"data: [DONE]\n\n")
    wfile.flush()
    trace("response.converted", request_id=request_id, output_items=len(output),
          output_text_len=len(text), usage=final.get("usage"), stream=True)


def caption_images_in_messages(chat_payload: Json, target_model: str, config: ProxyConfig, request_id: str) -> Json:
    """Replace image_url parts with MiMo-generated text captions. Routes turn to target_model after."""
    image_model = os.environ.get("CODEX_IMAGE_MODEL", IMAGE_MODEL_DEFAULT) or IMAGE_MODEL_DEFAULT
    messages = chat_payload.get("messages", [])

    # Collect all image URLs across messages for parallel captioning.
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

    # 1 image: call directly. 2+: parallel.
    if len(image_jobs) == 1:
        captions = [caption_image_via_mimo(image_jobs[0][2], image_model, config, request_id)]
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(image_jobs))) as pool:
            captions = list(pool.map(
                lambda job: caption_image_via_mimo(job[2], image_model, config, request_id),
                image_jobs,
            ))

    # Replace image parts with captions.
    for (mi, pi, _url), caption in zip(image_jobs, captions):
        messages[mi]["content"][pi] = {"type": "text", "text": f"[image: {caption}]"}

    # Collapse text-only lists back to strings (fast path for upstream).
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and all(
            isinstance(p, dict) and p.get("type") == "text" for p in content
        ):
            message["content"] = "\n".join(p.get("text", "") for p in content if p.get("text"))

    chat_payload["model"] = target_model
    trace("split_turn.captioned", request_id=request_id, captions=len(image_jobs), model=chat_payload["model"])
    return chat_payload


CAPTION_PROMPT = (
    "You are captioning a screenshot for a coding agent that cannot see images. "
    "In 2-3 sentences, describe: (1) what UI element/application is visible, "
    "(2) any text content — buttons, labels, error messages, code snippets (quote exactly), "
    "(3) layout structure — which panel/tab/section is active, where the cursor/focus is. "
    "Skip colors and styling unless they convey state (e.g. red error, green success)."
)


def caption_image_via_mimo(image_url: str, image_model: str, config: ProxyConfig, request_id: str) -> str:
    """Sub-call MiMo to caption a single image. Returns text description."""
    caption_payload: Json = {
        "model": image_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": CAPTION_PROMPT},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
        "stream": False,
        "max_tokens": 200,
    }
    try:
        chat = call_upstream_chat(caption_payload, config, request_id, timeout_sec=15.0)
        choice = (chat.get("choices") or [{}])[0]
        text = (choice.get("message", {}) or {}).get("content", "")
        return text.strip() if isinstance(text, str) and text.strip() else "[caption unavailable]"
    except ProxyError as exc:
        trace("split_turn.caption_failed", request_id=request_id, status=exc.status, message=exc.message[:200])
        return f"[caption failed: {exc.message[:100]}]"


def call_upstream_chat(chat_payload: Json, config: ProxyConfig, request_id: str, *, timeout_sec: float | None = None) -> Json:
    api_key = resolve_api_key(config, request_id)

    url = f"{config.chat_base_url}/chat/completions"
    raw_payload = json.dumps(chat_payload, separators=(",",":")).encode("utf-8")
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
    trace("upstream.start", request_id=request_id, url=url, bytes=len(raw_payload))
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout_sec or config.timeout_sec) as response:
            body = response.read()
            elapsed_ms = int((time.time() - started) * 1000)
            trace("upstream.done", request_id=request_id, status=response.status, bytes=len(body), elapsed_ms=elapsed_ms)
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ProxyError(HTTPStatus.BAD_GATEWAY, "upstream returned non-object JSON")
            return value
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        trace("upstream.error", request_id=request_id, status=exc.code, body=body[:2000])
        if exc.code == 429:
            retry_after = exc.headers.get("retry-after", "5")
            raise ProxyError(HTTPStatus.TOO_MANY_REQUESTS, f"rate limited (retry after {retry_after}s)") from exc
        if exc.code == 503:
            raise ProxyError(HTTPStatus.SERVICE_UNAVAILABLE, "upstream unavailable") from exc
        if exc.code == 504:
            raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "upstream timeout") from exc
        raise ProxyError(HTTPStatus.BAD_GATEWAY, f"upstream HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        trace("upstream.network_error", request_id=request_id, reason=str(exc.reason))
        raise ProxyError(HTTPStatus.BAD_GATEWAY, f"upstream network error: {exc.reason}") from exc


_api_key_cache: str | None = None
_api_key_lock = threading.Lock()


def resolve_api_key(config: ProxyConfig, request_id: str) -> str:
    global _api_key_cache
    if _api_key_cache:
        return _api_key_cache

    with _api_key_lock:
        if _api_key_cache:
            return _api_key_cache

        api_key = os.environ.get(config.api_key_env)
        if api_key:
            _api_key_cache = api_key
            trace("credential.source", request_id=request_id, source="env", env=config.api_key_env)
            return api_key

        keychain_service = os.environ.get("CODEX_KEYCHAIN_SERVICE", "opencode-go-api-key")
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

        raise ProxyError(HTTPStatus.UNAUTHORIZED, f"missing API key: set ${config.api_key_env} or keychain:{keychain_service}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Codex Responses API shim for OpenAI Chat Completions upstreams")
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
    args = build_parser().parse_args(argv)
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
    server = ThreadingHTTPServer((config.bind, config.port), DeepSeekResponsesHandler)
    server.config = config  # type: ignore[attr-defined]
    trace(
        "server.start",
        bind=config.bind,
        port=config.port,
        chat_base_url=config.chat_base_url,
        api_key_env=config.api_key_env,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        trace("server.stop", reason="keyboard_interrupt")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
