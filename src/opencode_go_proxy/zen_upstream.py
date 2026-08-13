"""Zen upstream client: per-family wire translation to opencode.ai/zen/v1.

The Zen gateway serves each model through one of four surfaces, picked by
``zen_families()[bare_id]``:

- openai_chat: POST /zen/v1/chat/completions with ``Authorization: Bearer``
- openai_responses: POST /zen/v1/responses with ``Authorization: Bearer``
- anthropic_messages: POST /zen/v1/messages with ``x-api-key``
- google_gemini: POST /zen/v1/models/<id> with ``x-goog-api-key``

Every Responses-API request is translated to the family's wire shape and the
family's SSE stream is translated back to Responses events, so the Codex app
sees one uniform Responses stream no matter which surface the model speaks.
The responses family is the exception: it is relayed verbatim. All zen turns
meter with ``provider="zen"`` so they never count against the opencode-go
quota. Zen requests are reachable only through the ``zen/`` model prefix; a
bare id that happens to match a zen model stays on the opencode-go path.
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
from .passthrough import _relay_stream
from .protocol import (
    DEFAULT_MODEL,
    _normalize_image_url,
    chat_completion_to_response,
    chat_message_to_response_output,
    flatten_content,
    inject_session_model,
    model_context_window,
    new_response_id,
    normalize_usage,
    now_unix,
    responses_input_to_chat_messages,
    responses_payload_to_chat_payload,
    responses_tools_to_chat_tools,
)
from .secrets import resolve_api_key
from .streaming import _ConnectFailed, _open_upstream_stream, keepalive_sec
from .trace import _mask_trace_body, trace
from .upstream import (
    default_max_retries,
    record_cache,
    retriable_http_status,
    retry_sleep,
    usage_tokens,
)
from .zen_catalog import ZEN_PREFIX, resolve_family, zen_families, zen_model_ids

Json = dict[str, Any]

ZEN_BASE_URL_DEFAULT = "https://opencode.ai/zen/v1"
ZEN_BASE_URL_ENV = "OPENCODE_ZEN_BASE_URL"
ZEN_PROVIDER = "zen"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_ANTHROPIC_MAX_TOKENS = 8192

# The four zen wire families; anything else is a catalog bug and must not be
# forwarded to an endpoint that will reject it.
ZEN_FAMILY_VALUES = frozenset(
    {"anthropic_messages", "google_gemini", "openai_responses", "openai_chat"}
)

# The chat-completions family streams DeepSeek-style reasoning deltas; the
# anthropic and gemini families send none for now.
REASONING_DELTA_KEYS = ("reasoning_content",)


def zen_base_url() -> str:
    """Zen upstream base: env override, then the opencode.ai default."""
    return (os.environ.get(ZEN_BASE_URL_ENV) or ZEN_BASE_URL_DEFAULT).rstrip("/")


def bare_zen_id(slug: str) -> str:
    """Strip the ``zen/`` provider prefix; any other slug is unchanged."""
    if slug.startswith(ZEN_PREFIX):
        return slug[len(ZEN_PREFIX):]
    return slug


def ensure_zen_slug(model: str) -> None:
    """Refuse to route a model without the zen prefix through the zen client.

    A bare slug that happens to match a zen id must stay on the opencode-go
    path; if one arrives here the caller routed wrong.
    """
    if not model.startswith(ZEN_PREFIX):
        raise ProxyError(
            HTTPStatus.BAD_REQUEST,
            f"zen routing requires the '{ZEN_PREFIX}' model prefix (got {model!r})",
        )


def zen_family_for(bare_id: str) -> str:
    """Zen wire family for a bare id.

    The persisted family map wins. An id the map misses but the model capture
    knows resolves from the id prefix (the catalog's own fallback), and
    anything else lands on the chat-completions surface zen uses for the rest.
    """
    family = zen_families().get(bare_id)
    if family in ZEN_FAMILY_VALUES:
        return family
    if bare_id in zen_model_ids():
        resolved = resolve_family(bare_id)
        if resolved in ZEN_FAMILY_VALUES:
            return resolved
    return "openai_chat"


def _user_agent() -> str:
    return os.environ.get("OPENCODE_GO_PROXY_USER_AGENT", "codex/1.0")


def _zen_endpoint(family: str, bare_id: str, *, stream: bool) -> str:
    """Per-family zen path for the bare model id."""
    base = zen_base_url()
    if family == "openai_responses":
        return f"{base}/responses"
    if family == "openai_chat":
        return f"{base}/chat/completions"
    if family == "anthropic_messages":
        return f"{base}/messages"
    quoted = urllib.parse.quote(bare_id, safe="")
    if stream:
        return f"{base}/models/{quoted}:streamGenerateContent?alt=sse"
    return f"{base}/models/{quoted}:generateContent"


def _zen_headers(family: str, api_key: str, *, stream: bool) -> dict[str, str]:
    """Per-family auth and accept headers."""
    accept = "text/event-stream" if stream else "application/json"
    headers = {
        "content-type": "application/json",
        "accept": accept,
        "user-agent": _user_agent(),
    }
    if family == "anthropic_messages":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    elif family == "google_gemini":
        headers["x-goog-api-key"] = api_key
    else:
        headers["authorization"] = f"Bearer {api_key}"
    return headers


def parse_zen_error(body: str) -> tuple[str | None, str | None]:
    """Extract (error_type, message) from a zen error envelope.

    Zen errors look like ``{"type":"error","error":{"type":"<Class>",
    "message":"..."},"metadata":{...}}``; the plain OpenAI
    ``{"error":{"message":...}}`` shape is accepted too. Unparseable bodies
    yield ``(None, None)`` and the caller falls back to a status message.
    """
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(value, dict):
        return None, None
    error = value.get("error")
    if isinstance(error, dict):
        error_type = error.get("type")
        message = error.get("message")
        return (
            error_type if isinstance(error_type, str) else None,
            message if isinstance(message, str) else None,
        )
    message = value.get("message")
    return None, message if isinstance(message, str) else None


def _build_zen_request(
    payload: Json,
    family: str,
    bare_id: str,
    api_key: str,
    *,
    stream: bool,
    session_model: str,
) -> tuple[str, Json, dict[str, str]]:
    """Return ``(url, body, headers)`` for one zen upstream call.

    ``session_model`` is the prefixed slug the app knows; the wire body always
    addresses the upstream with the bare id. Translated families get the
    session model injected into create_thread calls (spawned threads inherit
    the routed zen model); the verbatim responses family keeps the original
    body apart from the model strip.
    """
    if family == "openai_responses":
        working = dict(payload)
        working["model"] = bare_id
        url = _zen_endpoint(family, bare_id, stream=stream)
        return url, working, _zen_headers(family, api_key, stream=stream)

    if family == "openai_chat":
        working = inject_session_model(dict(payload), session_model)
        working["model"] = bare_id
        chat_payload, _request_model, _stats = responses_payload_to_chat_payload(working)
        # The catalog translation can rewrite the model (image fallback /
        # unknown slug); the zen upstream is addressed with the bare zen id,
        # never a substitute.
        chat_payload["model"] = bare_id
        if stream:
            chat_payload["stream"] = True
            chat_payload["stream_options"] = {"include_usage": True}
        else:
            chat_payload["stream"] = False
        url = _zen_endpoint(family, bare_id, stream=stream)
        return url, chat_payload, _zen_headers(family, api_key, stream=stream)

    if family == "anthropic_messages":
        working = inject_session_model(dict(payload), session_model)
        working["model"] = bare_id
        body = responses_payload_to_anthropic_payload(working)
        body["stream"] = stream
        url = _zen_endpoint(family, bare_id, stream=stream)
        return url, body, _zen_headers(family, api_key, stream=stream)

    working = inject_session_model(dict(payload), session_model)
    body = responses_payload_to_gemini_payload(working)
    url = _zen_endpoint(family, bare_id, stream=stream)
    return url, body, _zen_headers(family, api_key, stream=stream)


def _zen_post(
    url: str,
    raw_payload: bytes,
    headers: dict[str, str],
    config: ProxyConfig,
    request_id: str,
    *,
    max_retries: int | None = None,
) -> tuple[Json, int]:
    """POST one JSON zen request with the shared retry policy.

    Returns ``(parsed_json, retries)``. Transient failures (429 / 5xx /
    network / timeout) retry with the upstream.py backoff; terminal errors
    raise :class:`ProxyError` preserving the zen status code and envelope
    type/message. On 429 the message carries the upstream retry-after.
    """
    max_retries = default_max_retries() if max_retries is None else max_retries
    retries = 0
    while True:
        request = urllib.request.Request(url, data=raw_payload, headers=headers, method="POST")
        trace("zen.start", request_id=request_id, url=url, bytes=len(raw_payload), attempt=retries + 1)
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=config.timeout_sec) as response:
                body = response.read()
                elapsed_ms = int((time.time() - started) * 1000)
                trace("zen.done", request_id=request_id, status=response.status, bytes=len(body), elapsed_ms=elapsed_ms)
                try:
                    value = json.loads(body)
                except json.JSONDecodeError:
                    raise ProxyError(HTTPStatus.BAD_GATEWAY, "zen upstream returned invalid JSON", retries=retries)
                if not isinstance(value, dict):
                    raise ProxyError(HTTPStatus.BAD_GATEWAY, "zen upstream returned non-object JSON", retries=retries)
                return value, retries
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            trace("zen.error", request_id=request_id, status=exc.code, body=_mask_trace_body(body))
            if retriable_http_status(exc.code) and retries < max_retries:
                retries += 1
                trace("zen.retry", request_id=request_id, attempt=retries, status=exc.code)
                retry_sleep(retries)
                continue
            error_type, message = parse_zen_error(body)
            if exc.code == 429:
                retry_after = (exc.headers.get("retry-after", "5") if exc.headers else "5")
                raise ProxyError(
                    HTTPStatus.TOO_MANY_REQUESTS,
                    f"rate limited (retry after {retry_after}s)",
                    retries=retries,
                    upstream_status=exc.code,
                    error_type=error_type,
                ) from exc
            try:
                status = HTTPStatus(exc.code)
            except ValueError:
                status = HTTPStatus.BAD_GATEWAY
            raise ProxyError(
                status,
                message or f"zen upstream HTTP {exc.code}",
                retries=retries,
                upstream_status=exc.code,
                error_type=error_type,
            ) from exc
        except urllib.error.URLError as exc:
            trace("zen.network_error", request_id=request_id, reason=str(getattr(exc, "reason", exc)))
            if retries < max_retries:
                retries += 1
                trace("zen.retry", request_id=request_id, attempt=retries, reason=str(getattr(exc, "reason", exc)))
                retry_sleep(retries)
                continue
            raise ProxyError(HTTPStatus.BAD_GATEWAY, f"zen upstream network error: {getattr(exc, 'reason', exc)}", retries=retries) from exc
        except TimeoutError:
            trace("zen.timeout", request_id=request_id, timeout=config.timeout_sec)
            if retries < max_retries:
                retries += 1
                trace("zen.retry", request_id=request_id, attempt=retries, reason="timeout")
                retry_sleep(retries)
                continue
            raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "zen upstream timeout", retries=retries) from None


def _zen_post_verbatim(
    url: str,
    raw_payload: bytes,
    headers: dict[str, str],
    config: ProxyConfig,
    request_id: str,
    *,
    max_retries: int | None = None,
) -> tuple[int, bytes, int, str | None, str | None]:
    """POST one zen request and return ``(status, raw_body, retries, content_type, retry_after)``.

    The verbatim relay path: an upstream HTTP error is returned with the
    upstream's own status and body so the proxy relays it unchanged instead of
    substituting a ``proxy_error`` envelope. Network and timeout failures have
    no upstream body to relay, so they still raise :class:`ProxyError`.
    """
    max_retries = default_max_retries() if max_retries is None else max_retries
    retries = 0
    while True:
        request = urllib.request.Request(url, data=raw_payload, headers=headers, method="POST")
        trace("zen.start", request_id=request_id, url=url, bytes=len(raw_payload), attempt=retries + 1)
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=config.timeout_sec) as response:
                body = response.read()
                elapsed_ms = int((time.time() - started) * 1000)
                trace("zen.done", request_id=request_id, status=response.status, bytes=len(body), elapsed_ms=elapsed_ms)
                return response.status, body, retries, response.headers.get("content-type"), None
        except urllib.error.HTTPError as exc:
            body = exc.read()
            trace("zen.error", request_id=request_id, status=exc.code, body=_mask_trace_body(body.decode("utf-8", errors="replace")))
            if retriable_http_status(exc.code) and retries < max_retries:
                retries += 1
                trace("zen.retry", request_id=request_id, attempt=retries, status=exc.code)
                retry_sleep(retries)
                continue
            retry_after = exc.headers.get("retry-after") if exc.headers else None
            return exc.code, body, retries, (exc.headers.get("content-type") if exc.headers else None), retry_after
        except urllib.error.URLError as exc:
            trace("zen.network_error", request_id=request_id, reason=str(getattr(exc, "reason", exc)))
            if retries < max_retries:
                retries += 1
                trace("zen.retry", request_id=request_id, attempt=retries, reason=str(getattr(exc, "reason", exc)))
                retry_sleep(retries)
                continue
            raise ProxyError(HTTPStatus.BAD_GATEWAY, f"zen upstream network error: {getattr(exc, 'reason', exc)}", retries=retries) from exc
        except TimeoutError:
            trace("zen.timeout", request_id=request_id, timeout=config.timeout_sec)
            if retries < max_retries:
                retries += 1
                trace("zen.retry", request_id=request_id, attempt=retries, reason="timeout")
                retry_sleep(retries)
                continue
            raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "zen upstream timeout", retries=retries) from None


def _zen_tokens(value: Json, family: str) -> tuple[Any, Any, Any]:
    """(input, output, total) token counts from a family-shaped response."""
    if family == "openai_chat":
        usage = value.get("usage") if isinstance(value, dict) else None
        return usage_tokens(usage)
    usage = value.get("usage") if isinstance(value, dict) else None
    if family == "openai_responses" and isinstance(usage, dict):
        return usage.get("input_tokens"), usage.get("output_tokens"), usage.get("total_tokens")
    if family == "anthropic_messages" and isinstance(usage, dict):
        inp = usage.get("input_tokens")
        outp = usage.get("output_tokens")
        total = inp + outp if isinstance(inp, int) and isinstance(outp, int) else None
        return inp, outp, total
    metadata = value.get("usageMetadata") if isinstance(value, dict) else None
    if isinstance(metadata, dict):
        return (
            metadata.get("promptTokenCount"),
            metadata.get("candidatesTokenCount"),
            metadata.get("totalTokenCount"),
        )
    return None, None, None


def _usage_to_openai_shape(usage: Json) -> Json:
    """Normalize a family usage object to input/output/total token keys."""
    normalized: Json = {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }
    return {key: value for key, value in normalized.items() if value is not None}


def _image_url(part: Json) -> str | None:
    """Extract a safe image URL from a chat-style image part, if any."""
    normalized = _normalize_image_url(part)
    if normalized is None:
        return None
    url = (normalized.get("image_url") or {}).get("url")
    return url if isinstance(url, str) and url else None


def responses_payload_to_anthropic_payload(payload: Json) -> Json:
    """Translate a Responses payload to the Anthropic Messages shape.

    instructions and system messages become the top-level ``system`` list;
    chat messages map to anthropic roles (tool results become user messages
    with ``tool_result`` blocks, assistant tool calls become ``tool_use``
    content blocks); tools map to ``input_schema`` declarations. Consecutive
    same-role turns are allowed: the Anthropic API combines them.
    """
    messages, _stats = responses_input_to_chat_messages(payload)
    tools, _tool_stats = responses_tools_to_chat_tools(payload.get("tools"))

    system_parts: list[str] = []

    anthropic_messages: list[Json] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            text = flatten_content(content)
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            anthropic_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id") or "",
                            "content": _tool_result_text(content),
                        }
                    ],
                }
            )
            continue
        if role == "assistant":
            blocks: list[Json] = []
            text = flatten_content(content)
            if text:
                blocks.append({"type": "text", "text": text})
            for tool_call in message.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") or {}
                arguments = function.get("arguments", "{}")
                try:
                    parsed = json.loads(arguments) if isinstance(arguments, str) and arguments else {}
                except (ValueError, TypeError):
                    parsed = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool_call.get("id") or f"call_{uuid.uuid4().hex}",
                        "name": function.get("name", ""),
                        "input": parsed,
                    }
                )
            if blocks:
                anthropic_messages.append({"role": "assistant", "content": blocks})
            continue
        blocks = _anthropic_user_blocks(content)
        if blocks:
            anthropic_messages.append({"role": "user", "content": blocks})

    body: Json = {"model": "", "messages": anthropic_messages}
    if system_parts:
        body["system"] = system_parts
    max_tokens = payload.get("max_output_tokens")
    body["max_tokens"] = max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else DEFAULT_ANTHROPIC_MAX_TOKENS
    if payload.get("temperature") is not None:
        body["temperature"] = payload["temperature"]
    if tools:
        body["tools"] = [
            {
                "name": tool["function"]["name"],
                "description": tool["function"].get("description", ""),
                "input_schema": tool["function"].get("parameters") or {"type": "object", "properties": {}},
            }
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        ]
        tool_choice = _anthropic_tool_choice(payload.get("tool_choice"))
        if tool_choice is not None:
            body["tool_choice"] = tool_choice
    return body


def _tool_result_text(content: Any) -> str:
    """Flatten a chat tool result to the string anthropic tool_result wants."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return flatten_content(content)
    if content is None:
        return ""
    return json.dumps(content, separators=(",", ":"))


def _anthropic_user_blocks(content: Any) -> list[Json]:
    """Chat user content to anthropic content blocks (text + images)."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[Json] = []
    for part in content or []:
        if isinstance(part, str):
            if part:
                blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")
        if part_type in {"text", "input_text", "output_text"}:
            text = part.get("text")
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
            continue
        if part_type in {"image_url", "input_image", "image"}:
            url = _image_url(part)
            if url and url.startswith("data:"):
                header, _, data = url.partition(",")
                media_type = header[len("data:"):].partition(";")[0] or "image/png"
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
            elif url and url.startswith("https://"):
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return blocks


def _anthropic_tool_choice(choice: Any) -> Json | None:
    """Responses tool_choice to anthropic tool_choice; None means omit (auto)."""
    if isinstance(choice, str):
        if choice == "required":
            return {"type": "any"}
        return None
    if not isinstance(choice, dict):
        return None
    choice_type = choice.get("type")
    if choice_type == "function":
        name = choice.get("name")
        if isinstance(name, str) and name:
            return {"type": "tool", "name": name}
        return {"type": "any"}
    if choice_type == "required":
        return {"type": "any"}
    return None


def responses_payload_to_gemini_payload(payload: Json) -> Json:
    """Translate a Responses payload to the Gemini generateContent shape.

    Chat messages map to contents (assistant -> model, tool results -> user
    functionResponse parts, assistant tool calls -> functionCall parts);
    instructions and system messages become systemInstruction; tools become
    functionDeclarations. Consecutive same-role contents are merged, since
    Gemini rejects non-alternating roles.
    """
    messages, _stats = responses_input_to_chat_messages(payload)
    tools, _tool_stats = responses_tools_to_chat_tools(payload.get("tools"))

    system_texts: list[str] = []

    contents: list[Json] = []
    call_id_to_name: dict[str, str] = {}

    def append_content(role: str, parts: list[Json]) -> None:
        if not parts:
            return
        if contents and contents[-1].get("role") == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})

    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            text = flatten_content(content)
            if text:
                system_texts.append(text)
            continue
        if role == "tool":
            call_id = message.get("tool_call_id") or ""
            name = call_id_to_name.get(call_id, call_id or "tool")
            append_content(
                "user",
                [{"functionResponse": {"name": name, "response": _tool_result_value(content)}}],
            )
            continue
        gemini_role = "model" if role == "assistant" else "user"
        parts: list[Json] = []
        if isinstance(content, str):
            if content:
                parts.append({"text": content})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type in {"text", "input_text", "output_text"}:
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        parts.append({"text": text})
                elif part_type in {"image_url", "input_image", "image"}:
                    url = _image_url(part)
                    if url and url.startswith("data:"):
                        header, _, data = url.partition(",")
                        media_type = header[len("data:"):].partition(";")[0] or "image/png"
                        parts.append({"inlineData": {"mimeType": media_type, "data": data}})
        for tool_call in message.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function") or {}
            call_id = tool_call.get("id") or ""
            name = function.get("name", "")
            if call_id:
                call_id_to_name[call_id] = name
            try:
                arguments = json.loads(function.get("arguments", "{}")) if isinstance(function.get("arguments"), str) else {}
            except (ValueError, TypeError):
                arguments = {}
            parts.append({"functionCall": {"name": name, "args": arguments}})
        append_content(gemini_role, parts)

    body: Json = {"contents": contents}
    if system_texts:
        body["systemInstruction"] = {"parts": [{"text": text} for text in system_texts]}
    if tools:
        declarations = [
            {
                "name": tool["function"]["name"],
                "description": tool["function"].get("description", ""),
                "parameters": tool["function"].get("parameters") or {"type": "object", "properties": {}},
            }
            for tool in tools
            if isinstance(tool, dict) and isinstance(tool.get("function"), dict)
        ]
        if declarations:
            body["tools"] = [{"functionDeclarations": declarations}]
            tool_config = _gemini_tool_config(payload.get("tool_choice"), declarations)
            if tool_config is not None:
                body["toolConfig"] = tool_config
    generation_config: Json = {}
    if payload.get("temperature") is not None:
        generation_config["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        generation_config["topP"] = payload["top_p"]
    if payload.get("max_output_tokens") is not None:
        generation_config["maxOutputTokens"] = payload["max_output_tokens"]
    if generation_config:
        body["generationConfig"] = generation_config
    return body


def _tool_result_value(content: Any) -> Any:
    """Gemini functionResponse payload from a chat tool result."""
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (ValueError, TypeError):
            return {"value": content}
    if isinstance(content, list):
        return {"value": flatten_content(content)}
    if content is None:
        return {}
    return content


def _gemini_tool_config(choice: Any, declarations: list[Json]) -> Json | None:
    """Responses tool_choice to a Gemini functionCallingConfig; None means omit (AUTO)."""
    mode = "AUTO"
    allowed: list[str] | None = None
    if isinstance(choice, str):
        if choice == "required":
            mode = "ANY"
        elif choice == "none":
            mode = "NONE"
    elif isinstance(choice, dict):
        choice_type = choice.get("type")
        if choice_type == "function":
            name = choice.get("name")
            if isinstance(name, str) and name and any(d.get("name") == name for d in declarations):
                mode = "ANY"
                allowed = [name]
        elif choice_type == "required":
            mode = "ANY"
        elif choice_type == "none":
            mode = "NONE"
    config: Json = {"mode": mode}
    if allowed:
        config["allowedFunctionNames"] = allowed
    return {"functionCallingConfig": config}


def _anthropic_to_response(value: Json, model: str) -> Json:
    """Anthropic messages response to the Responses object shape."""
    fake, text_parts = _anthropic_content_to_fake(value.get("content"))
    output = chat_message_to_response_output(fake)
    usage = value.get("usage")
    normalized_usage = None
    if isinstance(usage, dict):
        inp = usage.get("input_tokens")
        outp = usage.get("output_tokens")
        total = inp + outp if isinstance(inp, int) and isinstance(outp, int) else None
        normalized_usage = normalize_usage(
            {"input_tokens": inp, "output_tokens": outp, "total_tokens": total}
        )
    return {
        "id": new_response_id(),
        "object": "response",
        "created_at": now_unix(),
        "status": "completed",
        "model": model,
        "output": output,
        "output_text": "".join(text_parts),
        "usage": normalized_usage,
    }


def _anthropic_content_to_fake(content: Any) -> tuple[Json, list[str]]:
    """Anthropic content blocks to (fake chat message, text parts)."""
    text_parts: list[str] = []
    tool_calls: list[Json] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type in {"text", "thinking"}:
            text = block.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
        elif block_type == "tool_use":
            arguments = block.get("input")
            tool_calls.append(
                {
                    "id": block.get("id") or f"call_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": (
                            arguments
                            if isinstance(arguments, str)
                            else json.dumps(arguments, separators=(",", ":"))
                        ),
                    },
                }
            )
    fake: Json = {}
    if text_parts:
        fake["content"] = "\n".join(text_parts)
    if tool_calls:
        fake["tool_calls"] = tool_calls
    return fake, text_parts


def _gemini_to_response(value: Json, model: str) -> Json:
    """Gemini generateContent response to the Responses object shape."""
    fake, text_parts = _gemini_content_to_fake(value)
    output = chat_message_to_response_output(fake)
    metadata = value.get("usageMetadata")
    normalized_usage = None
    if isinstance(metadata, dict):
        inp = metadata.get("promptTokenCount")
        outp = metadata.get("candidatesTokenCount")
        total = metadata.get("totalTokenCount")
        normalized_usage = normalize_usage(
            {"input_tokens": inp, "output_tokens": outp, "total_tokens": total}
        )
    return {
        "id": new_response_id(),
        "object": "response",
        "created_at": now_unix(),
        "status": "completed",
        "model": model,
        "output": output,
        "output_text": "".join(text_parts),
        "usage": normalized_usage,
    }


def _gemini_content_to_fake(value: Json) -> tuple[Json, list[str]]:
    """Gemini response to (fake chat message, text parts)."""
    text_parts: list[str] = []
    tool_calls: list[Json] = []
    candidates = value.get("candidates") if isinstance(value, dict) else None
    if not isinstance(candidates, list) or not candidates:
        return {}, text_parts
    first = candidates[0]
    content = first.get("content") if isinstance(first, dict) else None
    if not isinstance(content, dict):
        return {}, text_parts
    for part in content.get("parts") or []:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str) and text and not part.get("thought"):
            text_parts.append(text)
        function_call = part.get("functionCall")
        if isinstance(function_call, dict):
            tool_calls.append(
                {
                    "id": f"call_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {
                        "name": function_call.get("name", ""),
                        "arguments": json.dumps(function_call.get("args") or {}, separators=(",", ":")),
                    },
                }
            )
    fake: Json = {}
    if text_parts:
        fake["content"] = "\n".join(text_parts)
    if tool_calls:
        fake["tool_calls"] = tool_calls
    return fake, text_parts


def _translate_response(value: Json, family: str, model: str) -> Json:
    """Family-shaped non-stream response to the Responses object shape."""
    if family == "openai_responses":
        return value
    if family == "openai_chat":
        return chat_completion_to_response(value, request_model=model)
    if family == "anthropic_messages":
        return _anthropic_to_response(value, model)
    return _gemini_to_response(value, model)


def _meter_zen(
    model: str,
    started: float,
    status: int,
    *,
    input_tokens: Any = None,
    output_tokens: Any = None,
    total_tokens: Any = None,
    estimated_input_tokens: Any = None,
    stream_aborted: bool = False,
    empty_completion: bool = False,
    retries: int | None = None,
) -> None:
    """Append one usage event with the zen provider so zen turns never count
    against the opencode-go quota."""
    record_usage_event(
        model=model,
        status=status,
        duration_ms=int((time.time() - started) * 1000),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        estimated_input_tokens=estimated_input_tokens,
        stream_aborted=stream_aborted,
        empty_completion=empty_completion,
        retries=retries,
        provider=ZEN_PROVIDER,
    )


def _send_json(handler: Any, payload: Json, status: HTTPStatus = HTTPStatus.OK) -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _relay_upstream_error(
    handler: Any,
    status: int,
    body: bytes,
    headers: Any,
) -> None:
    """Relay an upstream error status and body before any SSE is committed.

    ``retry-after`` is forwarded when the upstream sent one (429 pacing).
    """
    content_type = (headers.get("content-type") if headers else None) or "application/json"
    handler.send_response(status)
    if headers:
        retry_after = headers.get("retry-after")
        if retry_after:
            handler.send_header("retry-after", retry_after)
    handler.send_header("content-type", content_type)
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


def call_zen_responses(payload: Json, config: ProxyConfig, request_id: str) -> Json:
    """Non-stream Responses call: translate per family and return the response.

    Metering is provider="zen" on success and on every ProxyError path; the
    error is re-raised for the dispatcher to render.
    """
    started = time.time()
    model = payload.get("model") or DEFAULT_MODEL
    bare_id = bare_zen_id(model)
    family = zen_family_for(bare_id)
    api_key = resolve_api_key(config, request_id)
    url, body, headers = _build_zen_request(
        payload, family, bare_id, api_key, stream=False, session_model=model
    )
    raw_payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    trace("zen.start", request_id=request_id, url=url, bytes=len(raw_payload), family=family, stream=False)
    try:
        value, retries = _zen_post(url, raw_payload, headers, config, request_id)
    except ProxyError as exc:
        _meter_zen(model, started, int(exc.status), retries=exc.retries or None)
        raise
    if family == "openai_chat":
        record_cache(config.cache_tracker, body.get("model"), value.get("usage"))
    inp, outp, total = _zen_tokens(value, family)
    _meter_zen(
        model, started, 200,
        input_tokens=inp, output_tokens=outp, total_tokens=total,
        retries=retries or None,
    )
    response = _translate_response(value, family, model)
    trace(
        "zen.done", request_id=request_id, family=family, stream=False,
        output_items=len(response.get("output", [])),
        output_text_len=len(response.get("output_text", "")),
    )
    return response


class _ZenStreamEngine:
    """Translate one zen SSE stream into Responses events.

    The engine owns the SSE write lock, the keepalive thread, monotonic
    output_index assignment, per-family chunk parsing, and zen metering. Tool
    calls accumulate during the stream and are emitted complete at finalize
    (the app reconciles items by id, so late emission is safe).
    """

    def __init__(
        self,
        handler: Any,
        payload: Json,
        config: ProxyConfig,
        request_id: str,
        *,
        family: str,
        bare_id: str,
        model: str,
        response_id: str,
        started: float,
        raw_payload: bytes,
    ) -> None:
        self.handler = handler
        self.config = config
        self.request_id = request_id
        self.family = family
        self.bare_id = bare_id
        self.model = model
        self.response_id = response_id
        self.started = started
        self.raw_payload = raw_payload
        self.retries = 0

        self.client_alive = True
        self.keepalive_stop = threading.Event()
        self.write_lock = threading.Lock()
        self.interval = keepalive_sec()
        self._ka_thread: threading.Thread | None = None

        self.message_id = f"msg_{uuid.uuid4().hex}"
        self.reasoning_id = f"rs_{uuid.uuid4().hex}"
        self.text = ""
        self.reasoning = ""
        self.tool_calls: list[Json] = []
        self.usage: Json | None = None
        self.got_data = False
        self.item_open = False
        self.reasoning_open = False
        self.msg_index: int | None = None
        self.reasoning_index = 0
        self.next_output_index = 0
        self._active_tool_index: int | None = None

    # -- SSE write plumbing -------------------------------------------------

    def _write(self, data: bytes) -> bool:
        try:
            with self.write_lock:
                self.handler.wfile.write(data)
                self.handler.wfile.flush()
            return True
        except (BrokenPipeError, OSError):
            return False

    def _keepalive(self) -> None:
        while not self.keepalive_stop.wait(self.interval):
            if not self.client_alive:
                return
            if not self._write(b": keepalive\n\n"):
                self.client_alive = False
                return

    def _start_keepalive(self) -> None:
        self._ka_thread = threading.Thread(target=self._keepalive, daemon=True)
        self._ka_thread.start()

    def _stop_keepalive(self) -> None:
        self.keepalive_stop.set()

    def send_event(self, event: Json) -> None:
        if not self.client_alive:
            return
        data = b"data: " + json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n\n"
        if not self._write(data):
            self.client_alive = False
            trace("client.disconnected", request_id=self.request_id, message="client closed connection during stream")

    def send_error(self, message: str, *, code: str | None = None) -> None:
        error: Json = {"message": message}
        if code:
            error["code"] = code
        self.send_event({"type": "response.error", "error": error})
        if self.client_alive:
            self._write(b"data: [DONE]\n\n")

    def allocate_index(self) -> int:
        index = self.next_output_index
        self.next_output_index += 1
        return index

    def _tokens(self) -> tuple[Any, Any, Any]:
        """(input, output, total) from the accumulated usage object.

        The chat family reports prompt/completion/total keys; anthropic and
        gemini usage is normalized to input/output/total as it accumulates.
        """
        if self.family == "openai_chat":
            return usage_tokens(self.usage)
        if isinstance(self.usage, dict):
            return (
                self.usage.get("input_tokens"),
                self.usage.get("output_tokens"),
                self.usage.get("total_tokens"),
            )
        return None, None, None

    def emit_reasoning(self, delta: str) -> None:
        if not self.reasoning_open:
            self.reasoning_index = self.allocate_index()
            self.send_event(
                {
                    "type": "response.output_item.added",
                    "output_index": self.reasoning_index,
                    "item": {
                        "type": "reasoning",
                        "id": self.reasoning_id,
                        "summary": [],
                        "status": "in_progress",
                    },
                }
            )
            self.reasoning_open = True
        self.reasoning += delta
        self.send_event(
            {
                "type": "response.reasoning_summary_text.delta",
                "item_id": self.reasoning_id,
                "output_index": self.reasoning_index,
                "summary_index": 0,
                "delta": delta,
            }
        )

    def emit_text(self, delta: str) -> None:
        if not self.item_open:
            self.msg_index = self.allocate_index()
            self.send_event(
                {
                    "type": "response.output_item.added",
                    "output_index": self.msg_index,
                    "item": {
                        "type": "message",
                        "id": self.message_id,
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                }
            )
            self.item_open = True
        self.text += delta
        self.send_event(
            {
                "type": "response.output_text.delta",
                "item_id": self.message_id,
                "output_index": self.msg_index,
                "delta": delta,
            }
        )

    # -- per-family chunk parsing -------------------------------------------

    def handle_chunk(self, chunk: Json) -> None:
        if self.family == "openai_chat":
            self._handle_chat_chunk(chunk)
        elif self.family == "anthropic_messages":
            self._handle_anthropic_chunk(chunk)
        else:
            self._handle_gemini_chunk(chunk)

    def _handle_chat_chunk(self, chunk: Json) -> None:
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return
        delta = choices[0].get("delta") or {}
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            self.emit_reasoning(reasoning)
        text = delta.get("content")
        if isinstance(text, str) and text:
            self.emit_text(text)
        tool_calls = delta.get("tool_calls")
        if isinstance(tool_calls, list):
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                index = tool_call.get("index", 0)
                while len(self.tool_calls) <= index:
                    self.tool_calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tool_call.get("id"):
                    self.tool_calls[index]["id"] = tool_call["id"]
                function = tool_call.get("function") or {}
                name_delta = function.get("name")
                if name_delta:
                    self.tool_calls[index]["function"]["name"] += name_delta
                arguments_delta = function.get("arguments")
                if arguments_delta:
                    self.tool_calls[index]["function"]["arguments"] += arguments_delta

    def _handle_anthropic_chunk(self, chunk: Json) -> None:
        chunk_type = chunk.get("type")
        if chunk_type == "message_start":
            message = chunk.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                self.usage = _usage_to_openai_shape(usage)
        elif chunk_type == "content_block_start":
            block = chunk.get("content_block") or {}
            if block.get("type") == "tool_use":
                self._active_tool_index = len(self.tool_calls)
                self.tool_calls.append(
                    {
                        "id": block.get("id") or f"call_{uuid.uuid4().hex}",
                        "type": "function",
                        "function": {"name": block.get("name", ""), "arguments": ""},
                    }
                )
        elif chunk_type == "content_block_delta":
            delta = chunk.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    self.emit_text(text)
            elif delta_type == "input_json_delta":
                partial = delta.get("partial_json")
                index = self._active_tool_index
                if isinstance(partial, str) and partial and index is not None and index < len(self.tool_calls):
                    self.tool_calls[index]["function"]["arguments"] += partial
        elif chunk_type == "message_delta":
            usage = chunk.get("usage")
            if isinstance(usage, dict) and usage.get("output_tokens") is not None:
                self.usage = dict(self.usage or {})
                self.usage["output_tokens"] = usage["output_tokens"]
                inp = self.usage.get("input_tokens")
                outp = usage["output_tokens"]
                if isinstance(inp, int) and isinstance(outp, int):
                    self.usage["total_tokens"] = inp + outp

    def _handle_gemini_chunk(self, chunk: Json) -> None:
        metadata = chunk.get("usageMetadata")
        if isinstance(metadata, dict):
            self.usage = {
                "input_tokens": metadata.get("promptTokenCount"),
                "output_tokens": metadata.get("candidatesTokenCount"),
                "total_tokens": metadata.get("totalTokenCount"),
            }
        candidates = chunk.get("candidates") or []
        if not candidates or not isinstance(candidates[0], dict):
            return
        content = candidates[0].get("content")
        if not isinstance(content, dict):
            return
        for part in content.get("parts") or []:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text and not part.get("thought"):
                self.emit_text(text)
            function_call = part.get("functionCall")
            if isinstance(function_call, dict):
                self.tool_calls.append(
                    {
                        "id": f"call_{uuid.uuid4().hex}",
                        "type": "function",
                        "function": {
                            "name": function_call.get("name", ""),
                            "arguments": json.dumps(function_call.get("args") or {}, separators=(",", ":")),
                        },
                    }
                )

    # -- attempt lifecycle --------------------------------------------------

    def _reset_accumulation(self) -> None:
        self.text = ""
        self.reasoning = ""
        self.tool_calls = []
        self.usage = None
        self.got_data = False
        self.item_open = False
        self.reasoning_open = False
        self.msg_index = None
        self.next_output_index = 0
        self._active_tool_index = None

    def run_attempt(self, req: urllib.request.Request) -> str:
        """Connect, stream, translate; returns 'content', 'empty', 'nodata', 'gone', or 'error'."""
        try:
            response, attempts = _open_upstream_stream(req, self.config, self.request_id, default_max_retries())
            self.retries += attempts
        except _ConnectFailed as fail:
            self.retries += fail.attempts
            exc = fail.exc
            if isinstance(exc, urllib.error.HTTPError):
                _relay_upstream_error(self.handler, exc.code, fail.body, exc.headers)
                _meter_zen(self.model, self.started, exc.code, retries=self.retries or None)
                return "error"
            if isinstance(exc, TimeoutError):
                _meter_zen(self.model, self.started, int(HTTPStatus.GATEWAY_TIMEOUT), retries=self.retries or None)
                raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "zen upstream timeout", retries=self.retries) from exc
            _meter_zen(self.model, self.started, int(HTTPStatus.BAD_GATEWAY), retries=self.retries or None)
            raise ProxyError(
                HTTPStatus.BAD_GATEWAY,
                f"zen upstream network error: {getattr(exc, 'reason', exc)}",
                retries=self.retries,
            ) from exc

        try:
            with response as resp:
                for line in resp:
                    if not self.client_alive:
                        break
                    line_text = line.decode("utf-8", errors="replace").strip()
                    if not line_text.startswith("data: "):
                        continue
                    self.got_data = True
                    data = line_text[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(chunk, dict):
                        self.handle_chunk(chunk)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            trace(
                "zen.stream_aborted",
                request_id=self.request_id,
                status=exc.code if isinstance(exc, urllib.error.HTTPError) else getattr(exc, "reason", str(exc)),
            )
            self.send_error("zen upstream stream aborted")
            _meter_zen(self.model, self.started, 502, stream_aborted=True, retries=self.retries or None)
            return "error"

        if not self.client_alive:
            trace("zen.client_gone", request_id=self.request_id, message="client disconnected before final events")
            _meter_zen(self.model, self.started, 0, stream_aborted=True, retries=self.retries or None)
            return "gone"
        if not self.got_data:
            self.send_error("zen upstream returned no SSE data")
            _meter_zen(self.model, self.started, 502, retries=self.retries or None)
            return "nodata"
        if not self.text and not self.tool_calls and not self.reasoning:
            return "empty"
        self.finalize()
        return "content"

    def finalize(self) -> None:
        fake: Json = {}
        if self.reasoning:
            fake["reasoning_content"] = self.reasoning
        if self.tool_calls:
            fake["tool_calls"] = self.tool_calls
        if self.text:
            fake["content"] = self.text
        output = chat_message_to_response_output(fake)

        if self.reasoning_open:
            reasoning_done: Json = {
                "type": "reasoning",
                "id": self.reasoning_id,
                "summary": [{"type": "summary_text", "text": self.reasoning}],
                "status": "completed",
            }
            self.send_event(
                {"type": "response.output_item.done", "output_index": self.reasoning_index, "item": reasoning_done}
            )
            for out_item in output:
                if out_item.get("type") == "reasoning":
                    out_item["id"] = self.reasoning_id

        tool_index = 0
        for item in output:
            if item.get("type") != "function_call":
                continue
            index = self.allocate_index()
            self.send_event({"type": "response.output_item.added", "output_index": index, "item": item})
            self.send_event({"type": "response.output_item.done", "output_index": index, "item": item})
            tool_index += 1

        if self.item_open and self.msg_index is not None:
            message_done: Json = {
                "type": "message",
                "id": self.message_id,
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": self.text, "annotations": []}],
            }
            self.send_event({"type": "response.output_item.done", "output_index": self.msg_index, "item": message_done})
            for out_item in output:
                if out_item.get("type") == "message":
                    out_item["id"] = self.message_id

        inp, outp, total = self._tokens()
        if isinstance(inp, int) and inp > 0:
            note_real_input_tokens(self.model)
        context_cap = model_context_window(self.model) or DEFAULT_ESTIMATE_CONTEXT_WINDOW
        estimated = estimate_input_tokens(self.model, len(self.raw_payload), self.usage, context_window=context_cap)
        final: Json = {
            "id": self.response_id,
            "object": "response",
            "created_at": now_unix(),
            "status": "completed",
            "model": self.model,
            "output": output,
            "output_text": self.text,
            "usage": normalize_usage(self.usage, estimated_input_tokens=estimated),
        }
        self.send_event({"type": "response.completed", "response": final})
        self._write(b"data: [DONE]\n\n")
        if self.family == "openai_chat":
            record_cache(self.config.cache_tracker, self.bare_id, self.usage)
        _meter_zen(
            self.model, self.started, 200,
            input_tokens=inp, output_tokens=outp, total_tokens=total,
            estimated_input_tokens=estimated,
            retries=self.retries or None,
        )
        trace(
            "zen.done", request_id=self.request_id, family=self.family, stream=True,
            output_items=len(output), output_text_len=len(self.text),
        )

    def run(self, req: urllib.request.Request) -> None:
        self.send_event(
            {
                "type": "response.created",
                "response": {
                    "id": self.response_id,
                    "object": "response",
                    "created_at": now_unix(),
                    "status": "in_progress",
                    "model": self.model,
                    "output": [],
                    "output_text": "",
                    "usage": None,
                },
            }
        )
        empty_attempts = 0
        while True:
            outcome = self.run_attempt(req)
            if outcome in ("error", "gone", "nodata"):
                return
            if outcome == "empty":
                empty_attempts += 1
                if empty_attempts == 1:
                    # A streamed 200 with no output is retried once with the
                    # identical request; response and item ids are reused.
                    self._reset_accumulation()
                    try:
                        _response, more = _open_upstream_stream(req, self.config, self.request_id, default_max_retries())
                        self.retries += more
                    except _ConnectFailed as fail:
                        self.retries += fail.attempts
                        exc = fail.exc
                        if isinstance(exc, urllib.error.HTTPError):
                            _relay_upstream_error(self.handler, exc.code, fail.body, exc.headers)
                            _meter_zen(self.model, self.started, exc.code, retries=self.retries or None)
                            return
                        if isinstance(exc, TimeoutError):
                            _meter_zen(self.model, self.started, int(HTTPStatus.GATEWAY_TIMEOUT), retries=self.retries or None)
                            raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "zen upstream timeout", retries=self.retries) from exc
                        _meter_zen(self.model, self.started, int(HTTPStatus.BAD_GATEWAY), retries=self.retries or None)
                        raise ProxyError(
                            HTTPStatus.BAD_GATEWAY,
                            f"zen upstream network error: {getattr(exc, 'reason', exc)}",
                            retries=self.retries,
                        ) from exc
                    continue
                self.send_error("zen upstream returned an empty completion", code="empty_completion")
                inp, outp, total = self._tokens()
                if isinstance(inp, int) and inp > 0:
                    note_real_input_tokens(self.model)
                context_cap = model_context_window(self.model) or DEFAULT_ESTIMATE_CONTEXT_WINDOW
                estimated = estimate_input_tokens(self.model, len(self.raw_payload), self.usage, context_window=context_cap)
                _meter_zen(
                    self.model, self.started, 200,
                    input_tokens=inp, output_tokens=outp, total_tokens=total,
                    estimated_input_tokens=estimated,
                    empty_completion=True, retries=self.retries or None,
                )
                return
            return


def handle_zen_responses_request(handler: Any, payload: Json, config: ProxyConfig, request_id: str) -> None:
    """Serve a /v1/responses request for a zen-routed model (stream and non-stream).

    The zen path owns the whole HTTP response: connect-first, so an upstream
    non-200 is answered with the upstream's own status and body before any SSE
    is committed. Streaming is per-family translated back to Responses events;
    the openai_responses family is relayed verbatim. Metering is provider="zen"
    on every outcome (success, upstream error, network error, client gone).
    """
    started = time.time()
    model = payload.get("model") or DEFAULT_MODEL
    ensure_zen_slug(model)
    bare_id = bare_zen_id(model)
    family = zen_family_for(bare_id)

    if payload.get("stream") is not True:
        # call_zen_responses meters provider="zen" on success and on every
        # ProxyError path, then re-raises for the dispatcher to render.
        _send_json(handler, call_zen_responses(payload, config, request_id))
        return

    api_key = resolve_api_key(config, request_id)
    url, body, headers = _build_zen_request(
        payload, family, bare_id, api_key, stream=True, session_model=model
    )
    raw_payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(url, data=raw_payload, headers=headers, method="POST")
    trace("zen.start", request_id=request_id, url=url, bytes=len(raw_payload), family=family, stream=True)
    try:
        response, retries = _open_upstream_stream(req, config, request_id, default_max_retries())
    except _ConnectFailed as fail:
        retries = fail.attempts
        exc = fail.exc
        if isinstance(exc, urllib.error.HTTPError):
            _relay_upstream_error(handler, exc.code, fail.body, exc.headers)
            _meter_zen(model, started, exc.code, retries=retries or None)
            return
        _meter_zen(
            model, started,
            int(HTTPStatus.GATEWAY_TIMEOUT if isinstance(exc, TimeoutError) else HTTPStatus.BAD_GATEWAY),
            retries=retries or None,
        )
        if isinstance(exc, TimeoutError):
            raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "zen upstream timeout", retries=retries) from exc
        raise ProxyError(HTTPStatus.BAD_GATEWAY, f"zen upstream network error: {getattr(exc, 'reason', exc)}", retries=retries) from exc

    handler.send_response(HTTPStatus.OK)
    handler.send_header("content-type", "text/event-stream")
    handler.send_header("cache-control", "no-cache")
    handler.end_headers()

    if family == "openai_responses":
        outcome = _relay_stream(response, handler, request_id)
        if outcome == "done":
            _meter_zen(model, started, 200, retries=retries or None)
        elif outcome == "gone":
            _meter_zen(model, started, 0, stream_aborted=True, retries=retries or None)
        else:
            _meter_zen(model, started, 502, stream_aborted=True, retries=retries or None)
        trace("zen.done", request_id=request_id, family=family, stream=True, outcome=outcome)
        return

    engine = _ZenStreamEngine(
        handler, payload, config, request_id,
        family=family, bare_id=bare_id, model=model,
        response_id=new_response_id(), started=started, raw_payload=raw_payload,
    )
    engine._start_keepalive()
    try:
        engine.run(req)
    finally:
        engine._stop_keepalive()


def handle_zen_chat_request(handler: Any, payload: Json, config: ProxyConfig, request_id: str) -> None:
    """Serve a /chat/completions request for a zen-routed model.

    Verbatim relay to the zen chat-completions surface with the zen key and
    base: the body, status, and stream are forwarded unchanged (only the model
    prefix is stripped). The SSE head is committed only after the upstream
    answers 200; non-200 upstream answers relay their own status and body.
    """
    started = time.time()
    model = payload.get("model") or DEFAULT_MODEL
    ensure_zen_slug(model)
    bare_id = bare_zen_id(model)
    api_key = resolve_api_key(config, request_id)
    body = dict(payload)
    body["model"] = bare_id
    url = _zen_endpoint("openai_chat", bare_id, stream=payload.get("stream") is True)
    raw_payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = _zen_headers("openai_chat", api_key, stream=payload.get("stream") is True)

    if payload.get("stream") is True:
        req = urllib.request.Request(url, data=raw_payload, headers=headers, method="POST")
        trace("zen.start", request_id=request_id, url=url, bytes=len(raw_payload), family="openai_chat", stream=True)
        try:
            response, retries = _open_upstream_stream(req, config, request_id, default_max_retries())
        except _ConnectFailed as fail:
            retries = fail.attempts
            exc = fail.exc
            if isinstance(exc, urllib.error.HTTPError):
                _relay_upstream_error(handler, exc.code, fail.body, exc.headers)
                _meter_zen(model, started, exc.code, retries=retries or None)
                return
            _meter_zen(
                model, started,
                int(HTTPStatus.GATEWAY_TIMEOUT if isinstance(exc, TimeoutError) else HTTPStatus.BAD_GATEWAY),
                retries=retries or None,
            )
            if isinstance(exc, TimeoutError):
                raise ProxyError(HTTPStatus.GATEWAY_TIMEOUT, "zen upstream timeout", retries=retries) from exc
            raise ProxyError(HTTPStatus.BAD_GATEWAY, f"zen upstream network error: {getattr(exc, 'reason', exc)}", retries=retries) from exc
        handler.send_response(HTTPStatus.OK)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("cache-control", "no-cache")
        handler.end_headers()
        outcome = _relay_stream(response, handler, request_id)
        if outcome == "done":
            _meter_zen(model, started, 200, retries=retries or None)
        elif outcome == "gone":
            _meter_zen(model, started, 0, stream_aborted=True, retries=retries or None)
        else:
            _meter_zen(model, started, 502, stream_aborted=True, retries=retries or None)
        trace("zen.done", request_id=request_id, family="openai_chat", stream=True, outcome=outcome)
        return

    status, raw, retries, content_type, retry_after = _zen_post_verbatim(
        url, raw_payload, headers, config, request_id
    )
    _meter_zen(model, started, status, retries=retries or None)
    handler.send_response(status)
    if retry_after:
        handler.send_header("retry-after", retry_after)
    handler.send_header("content-type", content_type or "application/json")
    handler.send_header("content-length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)
    handler.wfile.flush()
