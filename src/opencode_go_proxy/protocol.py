from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any


Json = dict[str, Any]

DEFAULT_MODEL = "deepseek-v4-flash"
IMAGE_MODEL_DEFAULT = "mimo-v2.5"

# Map OpenAI/Codex model slugs to OpenCode Go equivalents.
# When Codex sends a model not in the catalog, the alias map provides the replacement.
# If no alias exists, DEFAULT_MODEL is used.
# DeepSeek V4 Flash is the default — cheapest non-vision model on Go ($10/mo gets ~158k requests/mo).
MODEL_ALIASES: dict[str, str] = {
    "gpt-5.5": "deepseek-v4-pro",
    "gpt-5.4-mini": "deepseek-v4-flash",
    "gpt-5": "deepseek-v4-pro",
    "o3": "deepseek-v4-pro",
    "o4-mini": "deepseek-v4-flash",
    "codex-auto-review": "deepseek-v4-flash",
}


def _load_catalog_models() -> set[str]:
    """Load known model slugs from the catalog JSON file."""
    catalog_path = os.environ.get("CODEX_MODEL_CATALOG", os.path.expanduser("~/.codex/model-catalogs/deepseek-full.json"))
    try:
        with open(catalog_path) as f:
            catalog = json.load(f)
        return {m["slug"] for m in catalog.get("models", []) if isinstance(m, dict) and "slug" in m}
    except (OSError, json.JSONDecodeError, KeyError):
        return {DEFAULT_MODEL, IMAGE_MODEL_DEFAULT}


KNOWN_MODELS: set[str] = _load_catalog_models()


def new_response_id() -> str:
    return f"resp_{uuid.uuid4().hex}"


_ALLOWED_IMAGE_SCHEMES = ("data:image/", "https://")


def _is_safe_image_url(url: str) -> bool:
    """Reject non-http(s)/data URLs to prevent SSRF (file://, http://localhost, cloud metadata, etc)."""
    return url.startswith(_ALLOWED_IMAGE_SCHEMES)


def _normalize_image_url(part: Json) -> Json | None:
    """Coerce a Responses image part into an OpenAI Chat Completions image_url part.

    Handles: image_url as str, image_url as dict with .url, bare url key,
    MCP RawImageContent (type:"image" with data+mimeType), and bare base64 data URL strings.
    Returns None if no image can be derived or the URL scheme is not allowed.
    """
    image_url = part.get("image_url")
    if isinstance(image_url, str):
        return {"type": "image_url", "image_url": {"url": image_url}} if _is_safe_image_url(image_url) else None
    if isinstance(image_url, dict) and image_url.get("url"):
        url = image_url["url"]
        return {"type": "image_url", "image_url": image_url} if _is_safe_image_url(url) else None
    url = part.get("url")
    if isinstance(url, str):
        return {"type": "image_url", "image_url": {"url": url}} if _is_safe_image_url(url) else None
    # MCP RawImageContent: {"type":"image","data":"<base64>","mimeType":"image/png"}
    data = part.get("data")
    if isinstance(data, str) and data:
        mime = part.get("mimeType") or part.get("mime_type") or "image/png"
        if mime.startswith("data:"):
            if not mime.startswith("data:image/"):
                return None
            return {"type": "image_url", "image_url": {"url": mime}}
        return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}
    return None


def _is_data_url_string(s: str) -> bool:
    return isinstance(s, str) and s.startswith("data:image/") and "base64," in s


def _is_safe_url_string(s: str) -> bool:
    """Allow data:image/ and https:// URLs. Reject everything else (http://, file://, ftp://, etc)."""
    return _is_data_url_string(s) or s.startswith("https://")


def _content_to_chat_parts(content: Any) -> list[Json] | str:
    """Convert Responses content into OpenAI Chat Completions content parts.

    Returns a string when the content is text-only (the fast path used by the
    vast majority of turns), and a list of {type, text/image_url} dicts when
    image parts are present so the upstream multimodal model receives them.
    """
    if content is None or isinstance(content, str):
        if isinstance(content, str) and _is_safe_url_string(content):
            return [{"type": "image_url", "image_url": {"url": content}}]
        return content or ""
    if not isinstance(content, list):
        return flatten_content(content)

    has_image = any(
        isinstance(part, dict) and part.get("type") in {"input_image", "image_url", "image"}
        or (isinstance(part, str) and _is_safe_url_string(part))
        for part in content
    )
    if not has_image:
        return flatten_content(content)

    parts: list[Json] = []
    for part in content:
        if isinstance(part, str):
            if _is_safe_url_string(part):
                parts.append({"type": "image_url", "image_url": {"url": part}})
            elif part:
                parts.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            text = str(part)
            if text:
                parts.append({"type": "text", "text": text})
            continue
        ptype = part.get("type")
        if ptype in {"input_text", "output_text", "text"}:
            text = part.get("text", "")
            if isinstance(text, str) and text:
                parts.append({"type": "text", "text": text})
        elif ptype in {"input_image", "image_url", "image"}:
            img = _normalize_image_url(part)
            if img is not None:
                parts.append(img)
    return parts


def now_unix() -> int:
    return int(time.time())


def flatten_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
                continue
            # Responses sometimes distinguishes input_text/output_text by type
            # while keeping the text payload under the same key.
            if item.get("type") in {"input_text", "output_text"}:
                parts.append(str(item.get("text", "")))
        return "\n".join(part for part in parts if part)
    return str(content)


def flatten_summary(summary: Any) -> str:
    if not isinstance(summary, list):
        return flatten_content(summary)

    parts: list[str] = []
    for item in summary:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        text = item.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(part for part in parts if part)


def reasoning_content_from_item(item: Json) -> str:
    content = flatten_content(item.get("content", ""))
    if content:
        return content
    return flatten_summary(item.get("summary", ""))


def responses_input_to_chat_messages(payload: Json) -> tuple[list[Json], Json]:
    messages: list[Json] = []
    stats: Json = {
        "input_items": 0,
        "reasoning_items_dropped": 0,
        "reasoning_items_replayed": 0,
        "function_outputs": 0,
        "function_calls_replayed": 0,
    }

    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})

    input_value = payload.get("input", "")
    if isinstance(input_value, str):
        stats["input_items"] = 1
        messages.append({"role": "user", "content": input_value})
        return messages, stats

    if not isinstance(input_value, list):
        messages.append({"role": "user", "content": flatten_content(input_value)})
        stats["input_items"] = 1
        return messages, stats

    stats["input_items"] = len(input_value)
    pending_assistant_tool_calls: list[Json] = []
    pending_assistant_reasoning = ""
    pending_assistant_content = ""

    def attach_pending_reasoning(message: Json) -> Json:
        nonlocal pending_assistant_reasoning
        if pending_assistant_reasoning:
            message["reasoning_content"] = pending_assistant_reasoning
            pending_assistant_reasoning = ""
        return message

    def pending_assistant_message() -> Json:
        nonlocal pending_assistant_content
        message: Json = {
            "role": "assistant",
            "content": "",
            "tool_calls": pending_assistant_tool_calls,
        }
        pending_assistant_content = ""
        return attach_pending_reasoning(message)

    for item in input_value:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            messages.append({"role": "user", "content": str(item)})
            continue

        item_type = item.get("type")
        if item_type == "reasoning":
            reasoning = reasoning_content_from_item(item)
            if reasoning:
                pending_assistant_reasoning = (
                    f"{pending_assistant_reasoning}\n{reasoning}" if pending_assistant_reasoning else reasoning
                )
                stats["reasoning_items_replayed"] += 1
            else:
                stats["reasoning_items_dropped"] += 1
            continue

        if item_type == "function_call":
            pending_assistant_tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments", "{}"),
                    },
                }
            )
            stats["function_calls_replayed"] += 1
            continue

        if item_type == "function_call_output":
            if pending_assistant_tool_calls:
                messages.append(pending_assistant_message())
                pending_assistant_tool_calls = []
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or item.get("id") or "",
                    "content": _content_to_chat_parts(item.get("output", "")),
                }
            )
            stats["function_outputs"] += 1
            continue

        role = item.get("role", "user")
        if role == "developer":
            role = "system"
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        message: Json = {"role": role, "content": _content_to_chat_parts(item.get("content", ""))}
        if role == "assistant" and pending_assistant_tool_calls:
            content = message["content"]
            if isinstance(content, str) and content:
                pending_assistant_content = (
                    f"{pending_assistant_content}\n{content}" if pending_assistant_content else content
                )
            continue
        if role == "assistant":
            attach_pending_reasoning(message)
        if role == "tool" and item.get("tool_call_id"):
            message["tool_call_id"] = item["tool_call_id"]
        messages.append(message)

    if pending_assistant_tool_calls:
        messages.append(pending_assistant_message())
    elif pending_assistant_reasoning:
        messages.append(attach_pending_reasoning({"role": "assistant", "content": ""}))

    if not messages:
        messages.append({"role": "user", "content": ""})
    return messages, stats


def responses_tools_to_chat_tools(tools: Any) -> tuple[list[Json] | None, Json]:
    stats: Json = {"input_tools": 0, "forwarded_tools": 0, "dropped_tools": 0}
    if not isinstance(tools, list):
        return None, stats

    stats["input_tools"] = len(tools)
    chat_tools: list[Json] = []
    for tool in tools:
        if not isinstance(tool, dict):
            stats["dropped_tools"] += 1
            continue
        if tool.get("type") != "function":
            if tool.get("type") == "custom":
                name = tool.get("name")
                if not isinstance(name, str) or not name:
                    stats["dropped_tools"] += 1
                    continue
                description = tool.get("description", "")
                if not isinstance(description, str):
                    description = ""
                chat_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": (
                                f"{description}\n\n"
                                "This was a Responses custom/freeform tool. Provide JSON arguments "
                                "with an `input` string containing the raw tool input."
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "input": {
                                        "type": "string",
                                        "description": "Raw input for the custom/freeform tool.",
                                    }
                                },
                                "required": ["input"],
                                "additionalProperties": False,
                            },
                        },
                    }
                )
                stats["forwarded_tools"] += 1
                continue
            stats["dropped_tools"] += 1
            continue

        function = tool.get("function")
        if isinstance(function, dict):
            chat_tools.append({"type": "function", "function": function})
            stats["forwarded_tools"] += 1
            continue

        name = tool.get("name")
        if not isinstance(name, str) or not name:
            stats["dropped_tools"] += 1
            continue
        chat_tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                },
            }
        )
        stats["forwarded_tools"] += 1

    if not chat_tools:
        return None, stats
    return chat_tools, stats


def responses_payload_to_chat_payload(payload: Json) -> tuple[Json, str, Json]:
    messages, message_stats = responses_input_to_chat_messages(payload)
    tools, tool_stats = responses_tools_to_chat_tools(payload.get("tools"))

    incoming_model = payload.get("model", DEFAULT_MODEL)
    # Normalize: if model is in the alias map, use the DeepSeek equivalent.
    # If it's not a known catalog model and not aliased, fall back to DEFAULT_MODEL.
    if incoming_model in MODEL_ALIASES:
        incoming_model = MODEL_ALIASES[incoming_model]
    elif incoming_model not in KNOWN_MODELS:
        incoming_model = DEFAULT_MODEL
    # Detect images by scanning for actual image_url parts (not just list-shaped content).
    has_image = any(
        isinstance(m.get("content"), list)
        and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        for m in messages
    )
    image_model = os.environ.get("CODEX_IMAGE_MODEL", IMAGE_MODEL_DEFAULT) or IMAGE_MODEL_DEFAULT
    upstream_model = image_model if has_image else incoming_model

    chat_payload: Json = {
        "model": upstream_model,
        "messages": messages,
        "stream": False,
    }
    if tools is not None:
        chat_payload["tools"] = tools
        if payload.get("tool_choice") is not None:
            chat_payload["tool_choice"] = payload["tool_choice"]

    if payload.get("temperature") is not None:
        chat_payload["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        chat_payload["top_p"] = payload["top_p"]
    if payload.get("max_output_tokens") is not None:
        chat_payload["max_tokens"] = payload["max_output_tokens"]

    stats: Json = {
        "messages": message_stats,
        "tools": tool_stats,
        "upstream_model": upstream_model,
        "has_image": has_image,
        "tools_present": tools is not None,
    }
    return chat_payload, incoming_model, stats


def chat_completion_to_response(chat: Json, request_model: str | None = None) -> Json:
    response_id = new_response_id()
    model = request_model or DEFAULT_MODEL
    choice = _first_choice(chat)
    message = choice.get("message", {}) if isinstance(choice, dict) else {}
    output = chat_message_to_response_output(message)
    return {
        "id": response_id,
        "object": "response",
        "created_at": now_unix(),
        "status": "completed",
        "model": model,
        "output": output,
        "output_text": output_text_from_items(output),
        "usage": normalize_usage(chat.get("usage")),
    }


def chat_message_to_response_output(message: Json) -> list[Json]:
    output: list[Json] = []
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        output.append(
            {
                "type": "reasoning",
                "id": f"rs_{uuid.uuid4().hex}",
                "summary": [{"type": "summary_text", "text": reasoning}],
                "status": "completed",
            }
        )

    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function") or {}
        output.append(
            {
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex}",
                "call_id": tool_call.get("id") or f"call_{uuid.uuid4().hex}",
                "name": function.get("name", ""),
                "arguments": function.get("arguments", "{}"),
                "status": "completed",
            }
        )

    content = message.get("content")
    if isinstance(content, str) and content:
        output.append(
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": content, "annotations": []}],
            }
        )

    if not output:
        output.append(
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex}",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "", "annotations": []}],
            }
        )
    return output


def output_text_from_items(items: list[Json]) -> str:
    parts: list[str] = []
    for item in items:
        if item.get("type") != "message":
            continue
        parts.append(flatten_content(item.get("content", [])))
    return "".join(parts)


def normalize_usage(usage: Any) -> Json | None:
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens", input_tokens + output_tokens),
    }


def _first_choice(chat: Json) -> Json:
    choices = chat.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return {}
