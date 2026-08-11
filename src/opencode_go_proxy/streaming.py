"""The deep SSE streaming engine shared by /responses and /chat/completions.

Owns SSE framing, the keepalive thread (started before any caption sub-call
and kept alive until the stream truly ends), monotonic output_index
assignment, empty-completion retry, zero-input-token estimation, and usage
recording.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from typing import Any

from .config import ProxyConfig
from .errors import ProxyError
from .meter import (
    DEFAULT_ESTIMATE_CONTEXT_WINDOW,
    estimate_input_tokens,
    note_real_input_tokens,
    record_usage_event,
)
from .protocol import (
    DEFAULT_MODEL,
    cache_stats_from_usage,
    chat_message_to_response_output,
    inject_session_model,
    model_context_window,
    new_response_id,
    normalize_usage,
    now_unix,
    responses_payload_to_chat_payload,
)
from .quota import record_quota_from_headers
from .secrets import resolve_api_key
from .trace import _mask_trace_body, trace
from .upstream import (
    default_max_retries,
    record_cache,
    retriable_http_status,
    retry_sleep,
    usage_tokens,
)
from .vision import caption_images_in_messages, is_image_rejection_status

Json = dict[str, Any]

DEFAULT_KEEPALIVE_SEC = 15.0


def keepalive_sec() -> float:
    """Keepalive comment interval; override so tests can tick without a 15s wait."""
    try:
        return max(0.05, float(os.environ.get("OPENCODE_GO_PROXY_KEEPALIVE_SEC", str(DEFAULT_KEEPALIVE_SEC))))
    except ValueError:
        return DEFAULT_KEEPALIVE_SEC


def handle_streaming_request(payload: Json, config: ProxyConfig, request_id: str, wfile: Any) -> None:
    """Stream upstream response as SSE in real-time: created → text deltas → completed.

    Empty-completion safety: a streamed 200 that ends with no text, no tool
    call, and no reasoning is retried once with the identical request. Terminal
    events are held on the first attempt and response/item ids are reused, so a
    silently-empty upstream never looks like a successful turn; a second empty
    stream surfaces response.error code empty_completion.
    """
    session_model = payload.get("model") or DEFAULT_MODEL
    payload = inject_session_model(payload, session_model)
    chat_payload, request_model, conversion_stats = responses_payload_to_chat_payload(payload)

    client_alive = True
    interval = keepalive_sec()

    # Keepalive runs until the stream truly ends (upstream EOF, client
    # disconnect, error, or finalize), and every write is serialized through
    # one lock so a keepalive comment can never interleave inside a data frame.
    keepalive_stop = threading.Event()
    write_lock = threading.Lock()

    def _write(data: bytes) -> bool:
        try:
            with write_lock:
                wfile.write(data)
                wfile.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def keepalive() -> None:
        nonlocal client_alive
        while not keepalive_stop.wait(interval):
            if not client_alive:
                return
            if not _write(b": keepalive\n\n"):
                client_alive = False
                return

    ka_thread = threading.Thread(target=keepalive, daemon=True)
    ka_thread.start()

    try:
        if conversion_stats.get("has_image") and conversion_stats.get("tools_present"):
            chat_payload = caption_images_in_messages(chat_payload, request_model, config, request_id)
            conversion_stats["upstream_model"] = chat_payload.get("model")

        # Runtime image fallback (plan 009 escape hatch): a non-tools image turn
        # routed to a catalog-promised model can still be rejected by the
        # upstream (400/404/415/422). The first such rejection re-captions the
        # images and retries the same requested model once; the client sees one
        # stream either way.
        fell_back = False
        fallback_attempts = 0

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
            event_bytes = b"data: " + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n\n"
            if not _write(event_bytes):
                client_alive = False
                trace("client.disconnected", request_id=request_id, message="client closed connection during stream")

        def send_error(msg: str, *, code: str | None = None) -> None:
            error: Json = {"message": msg}
            if code:
                error["code"] = code
            send_event({"type": "response.error", "error": error})
            if client_alive:
                _write(b"data: [DONE]\n\n")

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

        def _make_req() -> urllib.request.Request:
            return urllib.request.Request(url, data=raw_payload, headers={
                "authorization": f"Bearer {api_key}", "content-type": "application/json",
                "accept": "text/event-stream",
                "user-agent": os.environ.get("OPENCODE_GO_PROXY_USER_AGENT", "codex/1.0"),
            }, method="POST")

        req = _make_req()
        trace("upstream.start", request_id=request_id, url=url, bytes=len(raw_payload), stream=True)
        started = time.time()

        # Accumulated state shared across attempts so an empty-completion retry
        # reuses the same response and item ids: the client never sees ghosts.
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
        total_retries = 0

        def run_attempt() -> str:
            """Run one upstream stream; returns 'content', 'empty', 'nodata', 'gone', or 'fallback'.

            'content'/'empty' split a streamed 200 by whether it produced any
            output; 'nodata' means the upstream opened but never sent SSE;
            'gone' means the client disconnected. 'fallback' means the image
            payload was rejected and the caller must re-run with captioned
            text. Terminal error paths send their own events, meter the turn,
            and return 'error' so the caller stops instead of retrying an
            upstream failure or a client abort.
            """
            nonlocal text, reasoning, tool_calls, tool_call_items, tool_call_open, usage
            nonlocal item_open, reasoning_open, reasoning_emitted, total_retries
            nonlocal req, raw_payload, chat_payload, fell_back, fallback_attempts

            # Per-attempt emission state; an empty attempt never opens items,
            # but resetting keeps a retry provably clean.
            text = ""
            reasoning = ""
            tool_calls = []
            tool_call_items = {}
            tool_call_open = set()
            item_open = False
            reasoning_open = False
            reasoning_emitted = False
            got_data = False

            def emit_tool_added(idx: int) -> None:
                """Emit output_item.added for tool call `idx` with its complete name.

                The upstream streams tool names in chunks (e.g. 'read_' then
                'file'), so added must be deferred until the name is complete.
                The stream moves on to the arguments phase only after the final
                name chunk, so the first arguments delta is the signal that the
                name is complete; tool calls whose name is still growing when
                the stream ends are emitted in the finalize phase below. A
                truncated name must never appear in added/done.
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

            max_retries = default_max_retries()
            # Connect with bounded retry on transient failures before any SSE
            # byte arrives. Once the response object is open we stream it below;
            # a failure inside that loop means the upstream died after its 200
            # head was already committed to the client, which we mark as an
            # aborted stream rather than a success.
            response = None
            while response is None:
                try:
                    response = urllib.request.urlopen(req, timeout=config.timeout_sec)
                    record_quota_from_headers(getattr(response, "headers", None))
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", errors="replace")
                    trace("upstream.error", request_id=request_id, status=exc.code, body=_mask_trace_body(body))
                    if retriable_http_status(exc.code) and total_retries < max_retries:
                        total_retries += 1
                        trace("upstream.retry", request_id=request_id, attempt=total_retries, status=exc.code)
                        retry_sleep(total_retries)
                        continue
                    if (
                        not fell_back
                        and conversion_stats.get("has_image")
                        and not conversion_stats.get("tools_present")
                        and is_image_rejection_status(exc.code)
                    ):
                        fell_back = True
                        fallback_attempts += 1
                        trace("image_fallback", request_id=request_id, kind="caption", status=exc.code,
                              model=request_model, stream=True)
                        chat_payload = caption_images_in_messages(chat_payload, request_model, config, request_id)
                        conversion_stats["upstream_model"] = chat_payload.get("model")
                        raw_payload = json.dumps(chat_payload, separators=(",",":")).encode("utf-8")
                        req = _make_req()
                        trace("upstream.start", request_id=request_id, url=url, bytes=len(raw_payload),
                              stream=True, fallback=True)
                        return "fallback"
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
                                       retries=total_retries + fallback_attempts)
                    return "error"
                except (urllib.error.URLError, TimeoutError) as exc:
                    trace("upstream.network_error", request_id=request_id, reason=str(getattr(exc, "reason", exc)))
                    if total_retries < max_retries:
                        total_retries += 1
                        trace("upstream.retry", request_id=request_id, attempt=total_retries, reason=str(getattr(exc, "reason", exc)))
                        retry_sleep(total_retries)
                        continue
                    send_error(f"upstream network error: {getattr(exc, 'reason', exc)}")
                    # Network failure: no upstream status exists (502) and nothing was
                    # streamed, so no streamAborted marker.
                    record_usage_event(model=model, status=502, duration_ms=int((time.time() - started) * 1000),
                                       retries=total_retries + fallback_attempts)
                    return "error"

            try:
                with response as resp:
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
                code = exc.code if isinstance(exc, urllib.error.HTTPError) else None
                trace("upstream.stream_aborted", request_id=request_id,
                      status=code or getattr(exc, "reason", str(exc)))
                send_error("upstream stream aborted")
                record_usage_event(model=model, status=502, duration_ms=int((time.time() - started) * 1000),
                                   stream_aborted=True, retries=total_retries + fallback_attempts)
                return "error"

            trace("upstream.done", request_id=request_id, status=200,
                  elapsed_ms=int((time.time() - started) * 1000), stream=True)
            if not client_alive:
                trace("client.gone", request_id=request_id, message="client disconnected before final events")
                record_usage_event(model=model, status=0, duration_ms=int((time.time() - started) * 1000),
                                   stream_aborted=True, retries=total_retries)
                return "gone"
            if not got_data:
                send_error("upstream returned no SSE data")
                record_usage_event(model=model, status=502, duration_ms=int((time.time() - started) * 1000),
                                   empty_completion=True, retries=total_retries + fallback_attempts)
                return "nodata"
            if not text and not tool_calls and not reasoning:
                return "empty"
            return "content"

        context_cap = model_context_window(model) or DEFAULT_ESTIMATE_CONTEXT_WINDOW
        empty_attempts = 0
        while True:
            outcome = run_attempt()
            if outcome in ("error", "gone", "nodata"):
                return
            if outcome == "fallback":
                continue
            if outcome == "empty":
                empty_attempts += 1
                if empty_attempts == 1:
                    # Hold terminal events and retry once with the identical
                    # request, reusing response and item ids.
                    continue
                duration_ms = int((time.time() - started) * 1000)
                send_error("upstream returned an empty completion", code="empty_completion")
                inp, outp, total = usage_tokens(usage)
                if isinstance(inp, int) and inp > 0:
                    note_real_input_tokens(model)
                estimated = estimate_input_tokens(model, len(raw_payload), usage, context_window=context_cap)
                record_usage_event(model=model, status=200, duration_ms=duration_ms,
                                   input_tokens=inp, output_tokens=outp, total_tokens=total,
                                   estimated_input_tokens=estimated,
                                   empty_completion=True, retries=total_retries + 1 + fallback_attempts)
                return
            break

        duration_ms = int((time.time() - started) * 1000)
        record_cache(config.cache_tracker, chat_payload.get("model"), usage)

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

        inp, outp, total = usage_tokens(usage)
        if isinstance(inp, int) and inp > 0:
            note_real_input_tokens(model)
        estimated = estimate_input_tokens(model, len(raw_payload), usage, context_window=context_cap)

        final: Json = {
            "id": response_id, "object": "response", "created_at": now_unix(),
            "status": "completed", "model": model, "output": output,
            "output_text": text, "usage": normalize_usage(usage, estimated_input_tokens=estimated),
        }
        send_event({"type": "response.completed", "response": final})
        _write(b"data: [DONE]\n\n")
        trace("response.converted", request_id=request_id, output_items=len(output),
              output_text_len=len(text), usage=final.get("usage"), stream=True,
              cache=cache_stats_from_usage(usage))
        empty = not text and not tool_calls and not reasoning
        record_usage_event(model=model, status=200, duration_ms=duration_ms,
                           input_tokens=inp, output_tokens=outp, total_tokens=total,
                           estimated_input_tokens=estimated,
                           retries=total_retries + empty_attempts + fallback_attempts, empty_completion=empty)
    finally:
        keepalive_stop.set()


def handle_chat_stream_passthrough(payload: Json, config: ProxyConfig, request_id: str, handler: Any) -> None:
    """Relay /chat/completions stream=true upstream SSE byte-for-byte.

    Connect-first: an upstream non-200 is answered with the upstream's own
    status and body before any SSE is committed. On 200 the proxy commits the
    SSE head, runs the keepalive comment thread until the relay truly ends
    (upstream EOF, client disconnect, or error), and writes every upstream line
    unchanged. Writes are serialized so a keepalive comment never interleaves
    inside a relayed frame, and the relay stops as soon as the client
    disconnects.
    """
    api_key = resolve_api_key(config, request_id)
    url = f"{config.chat_base_url}/chat/completions"
    raw_payload = json.dumps(payload, separators=(",",":")).encode("utf-8")
    req = urllib.request.Request(url, data=raw_payload, headers={
        "authorization": f"Bearer {api_key}", "content-type": "application/json",
        "accept": "text/event-stream",
        "user-agent": os.environ.get("OPENCODE_GO_PROXY_USER_AGENT", "codex/1.0"),
    }, method="POST")
    trace("upstream.start", request_id=request_id, url=url, bytes=len(raw_payload), stream=True)

    retries = 0
    max_retries = default_max_retries()
    response = None
    while response is None:
        try:
            response = urllib.request.urlopen(req, timeout=config.timeout_sec)
            record_quota_from_headers(getattr(response, "headers", None))
        except urllib.error.HTTPError as exc:
            body = exc.read()
            trace("upstream.error", request_id=request_id, status=exc.code,
                  body=_mask_trace_body(body.decode("utf-8", errors="replace")))
            if retriable_http_status(exc.code) and retries < max_retries:
                retries += 1
                trace("upstream.retry", request_id=request_id, attempt=retries, status=exc.code)
                retry_sleep(retries)
                continue
            # Nothing was committed yet, so the client sees the upstream's own
            # status and error body, not a proxy envelope.
            content_type = (exc.headers.get("content-type") if exc.headers else None) or "application/json"
            handler.send_response(exc.code)
            handler.send_header("content-type", content_type)
            handler.send_header("content-length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            handler.wfile.flush()
            return
        except urllib.error.URLError as exc:
            trace("upstream.network_error", request_id=request_id, reason=str(getattr(exc, "reason", exc)))
            if retries < max_retries:
                retries += 1
                trace("upstream.retry", request_id=request_id, attempt=retries, reason=str(getattr(exc, "reason", exc)))
                retry_sleep(retries)
                continue
            raise ProxyError(HTTPStatus.BAD_GATEWAY, f"upstream network error: {getattr(exc, 'reason', exc)}", retries=retries) from exc
        except TimeoutError:
            trace("upstream.timeout", request_id=request_id, timeout=config.timeout_sec)
            if retries < max_retries:
                retries += 1
                trace("upstream.retry", request_id=request_id, attempt=retries, reason="timeout")
                retry_sleep(retries)
                continue
            raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "upstream timeout", retries=retries) from None

    handler.send_response(HTTPStatus.OK)
    handler.send_header("content-type", "text/event-stream")
    handler.send_header("cache-control", "no-cache")
    handler.end_headers()

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

    started = time.time()
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
                    trace("client.disconnected", request_id=request_id,
                          message="client closed connection during stream")
                    break
    finally:
        keepalive_stop.set()

    elapsed_ms = int((time.time() - started) * 1000)
    trace("upstream.done", request_id=request_id, status=response.status, elapsed_ms=elapsed_ms, stream=True)
