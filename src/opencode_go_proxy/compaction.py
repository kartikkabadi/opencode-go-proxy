"""Native remote-compaction support for the Codex app (v1 and v2).

The Codex desktop app runs compaction turns through the same ``openai_base_url``
the proxy serves. Two wire families exist:

- v1: POST ``/responses/compact`` (or ``/v1/responses/compact``). The app
  parses the response's ``output`` items as the replacement conversation
  history; the summary is delivered as a single
  ``{"type": "compaction", "encrypted_content": ...}`` output item.
- v2: a normal ``/responses`` POST whose input ends in a compaction trigger
  item (``compaction_trigger`` or ``context_compaction``). The app streams the
  turn and expects exactly one ``context_compaction`` output item followed by
  ``response.completed``.

Both families summarize the conversation with one non-streaming chat
completion against the upstream the session's model routes to (opencode-go
chat completions, or the zen surface for ``zen/`` models), then encode the
summary with codex-router's ``kcr1:`` prefix + base64 format so a summary
written here round-trips through history and decodes on the router side (and
vice versa). The compaction turn is metered like any other routed turn, with
the summarization call's usage and the provider of the routed model.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from http import HTTPStatus
from typing import Any

from .config import ProxyConfig
from .errors import ProxyError
from .meter import record_usage_event
from .protocol import (
    DEFAULT_MODEL,
    flatten_content,
    known_models,
    new_response_id,
    output_text_from_items,
)
from .routing import normalize_model_slug, route_target
from .secrets import resolve_api_key
from .trace import trace
from .upstream import call_upstream_chat, usage_tokens
from .zen_upstream import (
    ZEN_PROVIDER,
    _build_zen_request,
    _translate_response,
    _zen_post,
    _zen_tokens,
    bare_zen_id,
    zen_family_for,
)

Json = dict[str, Any]

# The dedicated compaction endpoints (v1).
COMPACT_PATHS = frozenset({"/responses/compact", "/v1/responses/compact"})

# Input item types that mark a v2 remote-compaction request.
V2_TRIGGER_TYPES = frozenset({"context_compaction", "compaction_trigger"})

# Fixed summarization prompt (codex-router's COMPACT_PROMPT): a checkpoint
# compaction that produces a handoff summary for the resuming model.
COMPACT_PROMPT = (
    "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary "
    "for another language model that will resume the task.\n\n"
    "Include current progress, key decisions, constraints, user preferences, "
    "remaining steps, and critical data or references. Be concise, structured, "
    "and focused on seamless continuation."
)

# Encrypted-content envelope, matching codex-router's encodeSummary: the
# literal prefix "kcr1:" followed by base64(utf-8(summary)). A compaction
# item produced here decodes with codex-router's decodeSummary and vice
# versa, so history keeps working when the app talks to either proxy.
COMPACTION_PREFIX = "kcr1:"

# Transcript budget for the summarization input, mirroring codex-router's
# compactOutput: keep the tail (most recent) of the conversation.
TRANSCRIPT_BUDGET = 80_000

# When the summarization call returns no text, the compaction item still
# needs a meaningful payload (mirrors the router's "(no summary available)").
PLACEHOLDER_SUMMARY = "(no summary available)"


def encode_summary(summary: str) -> str:
    """Encode a summary in the codex-router wire format (``kcr1:`` + base64)."""
    return COMPACTION_PREFIX + base64.b64encode(summary.encode("utf-8")).decode("ascii")


def decode_summary(encrypted_content: Any) -> str | None:
    """Decode a ``kcr1:``-prefixed summary; None when the value is not ours."""
    if not isinstance(encrypted_content, str) or not encrypted_content.startswith(COMPACTION_PREFIX):
        return None
    try:
        return base64.b64decode(encrypted_content[len(COMPACTION_PREFIX):], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def has_compaction_trigger(payload: Json) -> bool:
    """True when the request's input ends in a v2 compaction trigger item.

    The app appends the trigger as the LAST input item (codex-rs pushes a
    ``compaction_trigger`` item after the history), while a normal turn whose
    history merely CONTAINS a ``context_compaction`` item from an earlier
    compaction keeps its current user message last. Testing only the last
    item detects the real request without hijacking every later turn. A
    malformed payload (missing or non-list input) falls through to the normal
    path.
    """
    input_value = payload.get("input")
    if not isinstance(input_value, list) or not input_value:
        return False
    last = input_value[-1]
    return isinstance(last, dict) and last.get("type") in V2_TRIGGER_TYPES


def render_transcript(input_value: Any, budget: int = TRANSCRIPT_BUDGET) -> str:
    """Flatten the input items to plain text, skipping trigger items.

    Each item's content is flattened with :func:`protocol.flatten_content` and
    joined; the result is bounded to the most recent ``budget`` characters
    (the tail), mirroring codex-router's compactOutput budget.
    """
    if isinstance(input_value, str):
        items: list[Any] = [input_value]
    elif isinstance(input_value, list):
        items = input_value
    else:
        items = []
    parts: list[str] = []
    for item in items:
        if isinstance(item, dict) and item.get("type") in V2_TRIGGER_TYPES:
            continue
        text = flatten_content(item.get("content") if isinstance(item, dict) else item)
        if text:
            parts.append(text)
    transcript = "\n\n".join(parts)
    if len(transcript) > budget:
        transcript = transcript[-budget:]
    return transcript


def _chat_text(chat: Json) -> str:
    """The assistant text of one non-stream chat-completions response."""
    choices = chat.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


def _responses_text(response: Json) -> str:
    """The output text of one Responses-shaped object."""
    output_text = response.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    return output_text_from_items(response.get("output", [])).strip()


def _summarize_go(model: str, transcript: str, config: ProxyConfig, request_id: str) -> tuple[str, Any, Any, Any, int]:
    """One non-stream opencode-go chat-completions summarization call."""
    bare = normalize_model_slug(model)
    if bare not in known_models():
        bare = DEFAULT_MODEL
    chat_payload: Json = {
        "model": bare,
        "messages": [
            {"role": "user", "content": transcript},
            {"role": "user", "content": COMPACT_PROMPT},
        ],
        "stream": False,
    }
    trace("compaction.summarize", request_id=request_id, target="opencode-go", model=bare, transcript_chars=len(transcript))
    chat, retries = call_upstream_chat(chat_payload, config, request_id)
    summary = _chat_text(chat) or PLACEHOLDER_SUMMARY
    inp, outp, total = usage_tokens(chat.get("usage"))
    return summary, inp, outp, total, retries


def _summarize_zen(model: str, transcript: str, config: ProxyConfig, request_id: str) -> tuple[str, Any, Any, Any, int]:
    """One non-stream zen summarization call (provider="zen", never double-metered)."""
    bare_id = bare_zen_id(model)
    family = zen_family_for(bare_id)
    api_key = resolve_api_key(config, request_id)
    zen_payload: Json = {
        "model": model,
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": transcript}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": COMPACT_PROMPT}]},
        ],
        "stream": False,
    }
    url, body, headers = _build_zen_request(
        zen_payload, family, bare_id, api_key, stream=False, session_model=model
    )
    raw_payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    trace("compaction.summarize", request_id=request_id, target="zen", model=bare_id, transcript_chars=len(transcript))
    value, retries = _zen_post(url, raw_payload, headers, config, request_id)
    summary = _responses_text(_translate_response(value, family, model)) or PLACEHOLDER_SUMMARY
    inp, outp, total = _zen_tokens(value, family)
    return summary, inp, outp, total, retries


def _summarize(
    payload: Json, model: str, config: ProxyConfig, request_id: str
) -> tuple[str, Any, Any, Any, int]:
    """Summarize the payload's input with one non-stream completion.

    Returns ``(summary, input_tokens, output_tokens, total_tokens, retries)``;
    token fields are the upstream's own numbers (None when the upstream sent
    none). Raises :class:`ProxyError` on upstream failure so the dispatcher
    surfaces the usual proxy error and the app can retry or fall back.
    """
    transcript = render_transcript(payload.get("input"))
    if route_target(model) == "zen":
        return _summarize_zen(model, transcript, config, request_id)
    return _summarize_go(model, transcript, config, request_id)


def _compaction_response(model: str, item: Json) -> Json:
    """The shared v1/v2 non-stream response envelope (Responses shape)."""
    return {
        "id": new_response_id(),
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": [item],
        "usage": None,
    }


def _send_json(handler: Any, payload: Json) -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)
    handler.wfile.flush()


def _write_compaction_sse(handler: Any, model: str, item: Json) -> None:
    """Stream the v2 compaction turn: created → added → done → completed.

    Event order mirrors codex-router's writeCompactionSse plus the
    ``response.output_item.added`` the app's v2 collector expects: the item
    must be announced before it can be done. Each event carries its
    ``sequence_number`` like the router's writer.
    """
    handler.send_response(HTTPStatus.OK)
    handler.send_header("content-type", "text/event-stream")
    handler.send_header("cache-control", "no-cache")
    handler.end_headers()
    created = {
        "id": new_response_id(),
        "object": "response",
        "created_at": int(time.time()),
        "status": "in_progress",
        "model": model,
        "output": [],
        "usage": None,
    }
    completed = {**created, "status": "completed", "output": [item]}
    events: list[tuple[str, Json]] = [
        ("response.created", {"response": created}),
        ("response.output_item.added", {"output_index": 0, "item": item}),
        ("response.output_item.done", {"output_index": 0, "item": item}),
        ("response.completed", {"response": completed}),
    ]
    for sequence, (event_type, data) in enumerate(events):
        event: Json = {"type": event_type, "sequence_number": sequence, **data}
        event_data = json.dumps(event, separators=(",", ":"))
        handler.wfile.write(f"event: {event_type}\ndata: {event_data}\n\n".encode())
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


def handle_compaction(
    handler: Any,
    payload: Json,
    config: ProxyConfig,
    request_id: str,
    *,
    path: str,
) -> None:
    """Serve one remote-compaction request (v1 JSON, or v2 stream/JSON).

    The summarization sub-call is metered as the compaction turn itself
    (provider follows the routed model: opencode-go default or zen), with the
    upstream usage numbers when present; an upstream failure is metered at the
    surfaced status and re-raised for the dispatcher's error envelope.
    """
    started = time.time()
    v2 = path not in COMPACT_PATHS
    model = payload.get("model") or DEFAULT_MODEL
    provider = ZEN_PROVIDER if route_target(model) == "zen" else None
    try:
        summary, inp, outp, total, retries = _summarize(payload, model, config, request_id)
    except ProxyError as exc:
        record_usage_event(
            model=model,
            status=int(exc.status),
            duration_ms=int((time.time() - started) * 1000),
            retries=exc.retries or None,
            provider=provider,
        )
        raise
    encoded = encode_summary(summary)
    item: Json = {
        "type": "context_compaction" if v2 else "compaction",
        "id": f"cmp_{uuid.uuid4().hex}",
        "encrypted_content": encoded,
    }
    record_usage_event(
        model=model,
        status=200,
        duration_ms=int((time.time() - started) * 1000),
        input_tokens=inp,
        output_tokens=outp,
        total_tokens=total,
        retries=retries or None,
        provider=provider,
    )
    trace(
        "compaction.done",
        request_id=request_id,
        v2=v2,
        model=model,
        summary_chars=len(summary),
        encrypted_chars=len(encoded),
    )
    if not v2:
        _send_json(handler, _compaction_response(model, item))
        return
    if payload.get("stream") is True:
        _write_compaction_sse(handler, model, item)
    else:
        _send_json(handler, _compaction_response(model, item))
