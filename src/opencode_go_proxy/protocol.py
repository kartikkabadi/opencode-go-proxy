from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from .codex_tools import merge_codex_app_tools

Json = dict[str, Any]


def _as_int(value: Any) -> int:
    """Coerce an upstream token count to int, tolerating malformed values.

    Upstream usage may carry string or missing token fields; a malformed value
    must never crash a request that was already billed upstream.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


# The only codex_app tool whose live input schema accepts a model override:
# create_thread. fork_thread / handoff_thread declare additionalProperties:
# false without a model key, so injecting one would make the app reject the
# call, and the live codex-router reference (SPAWN_MODEL_TOOLS) rewrites
# create_thread only. send_message_to_thread keeps its own settings.
SESSION_SPAWN_TOOLS = {"create_thread"}


def _translate_tool_choice(tool_choice: Any) -> Any:
    """Map a Responses tool_choice to the Chat Completions shape.

    Responses uses {"type": "function", "name": X} and {"type": "auto|required|none"};
    Chat Completions needs {"type": "function", "function": {"name": X}} and the
    bare strings "auto" / "required" / "none". Anything unrecognized is dropped
    so the upstream applies its own default rather than failing on a bad shape.
    """
    if isinstance(tool_choice, str):
        return tool_choice
    if isinstance(tool_choice, dict):
        t = tool_choice.get("type")
        if t == "function":
            name = tool_choice.get("name")
            if isinstance(name, str) and name:
                return {"type": "function", "function": {"name": name}}
            return None
        if t in ("auto", "required", "none"):
            return t
    return None


def _session_spawn_name(item: Json) -> str | None:
    """Return the bare tool name of a function_call item, if it is a session-spawn call.

    Only genuine codex_app thread-spawn tools match, so unrelated MCP tools
    (e.g. mcp__slack__create_thread) are never rewritten. Accepted forms:
    - flat: name is "codex_app__create_thread"
    - namespaced: namespace is absent or "codex_app" with a bare spawn-tool name
    """
    name = item.get("name")
    if not isinstance(name, str):
        return None
    if "__" in name:
        namespace, _, bare = name.partition("__")
        if namespace != "codex_app" or bare not in SESSION_SPAWN_TOOLS:
            return None
        return bare
    if item.get("namespace") not in (None, "codex_app"):
        return None
    if name not in SESSION_SPAWN_TOOLS:
        return None
    return name


def inject_session_model(payload: Json, session_model: str) -> Json:
    """Inject the session's model into create_thread calls.

    Spawned threads inherit the routed session's model instead of falling back
    to the native Codex model (which is quota-blocked for this account). Only
    applies when the call omits an explicit model; other tools and
    chatgptWorkCloud targets are untouched.
    """
    if not isinstance(payload, dict):
        return payload
    input_value = payload.get("input")
    if not isinstance(input_value, list):
        return payload

    changed = False
    for item in input_value:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        if _session_spawn_name(item) is None:
            continue
        arguments = item.get("arguments")
        if not isinstance(arguments, str):
            continue
        try:
            parsed = json.loads(arguments)
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, dict):
            continue
        target = parsed.get("target")
        if isinstance(target, dict) and target.get("type") == "chatgptWorkCloud":
            continue
        model = parsed.get("model")
        if model is not None and model != "":
            continue
        if model is None and "model" in parsed:
            # {"model": null} is explicitly present — leave it as-is.
            continue
        parsed["model"] = session_model
        item["arguments"] = json.dumps(parsed, sort_keys=True)
        changed = True

    if not changed:
        return payload
    return dict(payload)

DEFAULT_MODEL = "deepseek-v4-flash"
IMAGE_MODEL_DEFAULT = "mimo-v2.5"

MODEL_ALIASES: dict[str, str] = {
    "gpt-5.5": "deepseek-v4-pro",
    "gpt-5.4-mini": "deepseek-v4-flash",
    "gpt-5": "deepseek-v4-pro",
    "o3": "deepseek-v4-pro",
    "o4-mini": "deepseek-v4-flash",
    "codex-auto-review": "deepseek-v4-flash",
}




def _catalog_mtime() -> tuple[str, int | None]:
    """(path, mtime_ns) for the catalog the proxy serves; mtime None when missing."""
    from opencode_go_proxy import catalog as _catalog

    path = _catalog.default_catalog_path()
    try:
        return path, os.stat(path).st_mtime_ns
    except OSError:
        return path, None


_KNOWN_MODELS_CACHE: tuple[str, int | None, set[str]] | None = None


def known_models() -> set[str]:
    """Return the live set of known model slugs, cached by catalog file mtime.

    Runtime refresh rewrites the state-dir catalog, so a changed file mtime
    makes the next call re-read without a restart. reload_known_models()
    bypasses the cache explicitly.
    """
    global _KNOWN_MODELS_CACHE
    from opencode_go_proxy import catalog as _catalog

    path, mtime = _catalog_mtime()
    if (
        _KNOWN_MODELS_CACHE is not None
        and _KNOWN_MODELS_CACHE[0] == path
        and _KNOWN_MODELS_CACHE[1] == mtime
    ):
        return _KNOWN_MODELS_CACHE[2]
    slugs = _catalog.load_known_slugs(catalog_path=path)
    _KNOWN_MODELS_CACHE = (path, mtime, slugs)
    return slugs


def reload_known_models() -> set[str]:
    """Drop the mtime caches and re-read known slugs from the catalog."""
    global _KNOWN_MODELS_CACHE, _IMAGE_CAPABLE_CACHE, _CONTEXT_WINDOW_CACHE
    _KNOWN_MODELS_CACHE = None
    _IMAGE_CAPABLE_CACHE = None
    _CONTEXT_WINDOW_CACHE = None
    return known_models()


_CONTEXT_WINDOW_CACHE: tuple[str, int | None, dict[str, int]] | None = None


def model_context_window(model: str) -> int | None:
    """Best-known context window for a catalog model; None when unknown.

    Reads the full-shape catalog the same way known_models() does, cached by
    file mtime. Used to cap zero-input-token estimates at the model's real
    window instead of a proxy-wide default.
    """
    global _CONTEXT_WINDOW_CACHE

    path, mtime = _catalog_mtime()
    if (
        _CONTEXT_WINDOW_CACHE is not None
        and _CONTEXT_WINDOW_CACHE[0] == path
        and _CONTEXT_WINDOW_CACHE[1] == mtime
    ):
        windows = _CONTEXT_WINDOW_CACHE[2]
    else:
        windows = {}
        try:
            with open(path) as f:
                catalog = json.load(f)
            for entry in catalog.get("models", []):
                if not isinstance(entry, dict):
                    continue
                slug = entry.get("slug")
                context = entry.get("context_window")
                if isinstance(slug, str) and slug and isinstance(context, int) and context > 0:
                    windows[slug] = context
        except (OSError, json.JSONDecodeError):
            pass
        _CONTEXT_WINDOW_CACHE = (path, mtime, windows)
    return windows.get(model)


_IMAGE_CAPABLE_CACHE: tuple[str, int | None, set[str]] | None = None


def image_capable_models() -> set[str]:
    """Return catalog slugs whose record declares image input, cached by mtime.

    Mirrors known_models(): a rewritten catalog file makes the next call
    re-read. Used to keep image turns on the requested model when it can
    actually accept images, instead of always forcing the image default.
    """
    global _IMAGE_CAPABLE_CACHE

    path, mtime = _catalog_mtime()
    if (
        _IMAGE_CAPABLE_CACHE is not None
        and _IMAGE_CAPABLE_CACHE[0] == path
        and _IMAGE_CAPABLE_CACHE[1] == mtime
    ):
        return _IMAGE_CAPABLE_CACHE[2]
    slugs: set[str] = set()
    try:
        with open(path) as f:
            catalog = json.load(f)
        for entry in catalog.get("models", []):
            if not isinstance(entry, dict):
                continue
            slug = entry.get("slug")
            modalities = entry.get("input_modalities")
            if (
                isinstance(slug, str)
                and slug
                and isinstance(modalities, list)
                and "image" in modalities
            ):
                slugs.add(slug)
    except (OSError, json.JSONDecodeError):
        pass
    _IMAGE_CAPABLE_CACHE = (path, mtime, slugs)
    return slugs


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


def _is_safe_url_string(s: str) -> bool:
    """Allow data:image/...base64, and https:// URLs. Reject everything else (http://, file://, ftp://, etc)."""
    return isinstance(s, str) and (s.startswith("https://") or (s.startswith("data:image/") and "base64," in s))


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


def reasoning_content_from_item(item: Json) -> str:
    content = flatten_content(item.get("content", ""))
    if content:
        return content
    return flatten_content(item.get("summary", ""))


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
            ns = item.get("namespace")
            name = item.get("name", "")
            flat_name = f"{ns}__{name}" if ns else name
            pending_assistant_tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {
                        "name": flat_name,
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

    # Requests that carry a tools array get the codex_app snapshot merged in
    # first, so a routed model can spawn threads even when the app sent only a
    # reduced codex_app namespace (deferred schemas).
    tools = merge_codex_app_tools(tools)
    stats["input_tools"] = len(tools)
    chat_tools: list[Json] = []
    for tool in tools:
        if not isinstance(tool, dict):
            stats["dropped_tools"] += 1
            continue
        tt = tool.get("type")

        # Namespace tools (MCP servers): flatten sub-tools with namespace prefix.
        if tt == "namespace":
            ns_name = tool.get("name", "")
            sub_tools = tool.get("tools") or []
            if not isinstance(sub_tools, list):
                stats["dropped_tools"] += 1
                continue
            for sub in sub_tools:
                if not isinstance(sub, dict) or sub.get("type") != "function":
                    stats["dropped_tools"] += 1
                    continue
                sub_name = sub.get("name")
                if not isinstance(sub_name, str) or not sub_name:
                    stats["dropped_tools"] += 1
                    continue
                full_name = f"{ns_name}__{sub_name}"
                fn = sub.get("function") or {
                    "name": full_name,
                    "description": sub.get("description", ""),
                    "parameters": sub.get("parameters", {"type": "object", "properties": {}}),
                }
                fn = dict(fn)
                fn["name"] = full_name
                chat_tools.append({"type": "function", "function": fn})
                stats["forwarded_tools"] += 1
            continue

        if tt != "function":
            if tt == "web_search_preview":
                # The app's web search tool arrives as a bare type entry with no
                # name or schema; the upstream model needs a callable function,
                # and the app dispatches the returned call by this exact name.
                chat_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": "web_search_preview",
                            "description": (
                                "Search the web for current information. Use it when the user "
                                "asks a question that needs up-to-date or external information."
                            ),
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "query": {
                                        "type": "string",
                                        "description": "The search query.",
                                    }
                                },
                                "required": ["query"],
                                "additionalProperties": False,
                            },
                        },
                    }
                )
                stats["forwarded_tools"] += 1
                continue
            if tt == "custom":
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
    # Detect images by scanning for actual image_url parts (not just list-shaped content).
    has_image = any(
        isinstance(m.get("content"), list)
        and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        for m in messages
    )
    # Dispatch relays native slugs to the passthrough before translation; if
    # one arrives here anyway, its model is never rewritten. For opencode-go
    # targets the prefixed slug is checked against the catalog by its bare
    # form, and the upstream chat payload addresses the provider with the
    # bare slug (the reference router's upstreamModel). Unknown non-native
    # slugs fall back to DEFAULT_MODEL, exactly as before the alias map died.
    if incoming_model in MODEL_ALIASES:
        incoming_model = MODEL_ALIASES[incoming_model]
    elif incoming_model not in known_models():
        incoming_model = DEFAULT_MODEL
    # Detect images by scanning for actual image_url parts (not just list-shaped content).
    has_image = any(
        isinstance(m.get("content"), list)
        and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        for m in messages
    )
    if has_image:
        if incoming_model in image_capable_models():
            upstream_model = incoming_model
        else:
            image_model = (
                os.environ.get("CODEX_IMAGE_MODEL", IMAGE_MODEL_DEFAULT) or IMAGE_MODEL_DEFAULT
            )
            upstream_model = image_model
    else:
        upstream_model = incoming_model

    chat_payload: Json = {
        "model": upstream_model,
        "messages": messages,
        "stream": False,
    }
    if tools is not None:
        chat_payload["tools"] = tools
        if payload.get("tool_choice") is not None:
            choice = _translate_tool_choice(payload["tool_choice"])
            if choice is not None:
                chat_payload["tool_choice"] = choice

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


def chat_completion_to_response(
    chat: Json, request_model: str | None = None, *, estimated_input_tokens: int | None = None
) -> Json:
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
        "usage": normalize_usage(chat.get("usage"), estimated_input_tokens=estimated_input_tokens),
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
        flat_name = function.get("name", "")
        # Split flat name back into namespace + name for Codex.
        # Codex's ResponseItem::FunctionCall has separate namespace and name fields.
        # Namespaced tools are flattened as {namespace}__{name}; split on last "__".
        namespace, name = None, flat_name
        if "__" in flat_name:
            ns, _, n = flat_name.rpartition("__")
            if ns and n:
                namespace, name = ns, n
        item: Json = {
            "type": "function_call",
            "id": f"fc_{uuid.uuid4().hex}",
            "call_id": tool_call.get("id") or f"call_{uuid.uuid4().hex}",
            "name": name,
            "arguments": function.get("arguments", "{}"),
            "status": "completed",
        }
        if namespace:
            item["namespace"] = namespace
        output.append(item)

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


def cache_stats_from_usage(usage: Any) -> Json:
    """Extract prefix-cache accounting from an upstream usage object.

    DeepSeek-style chat completions report prompt_cache_hit_tokens and
    prompt_cache_miss_tokens; OpenAI-compatible endpoints report
    prompt_tokens_details.cached_tokens. Whichever shape the upstream uses,
    the proxy needs the same two numbers to compute the hit ratio it exposes
    on /cache. Returns a dict with hit, miss, and ratio (0..1); ratio is
    None when the upstream reported no cache fields at all.
    """
    if not isinstance(usage, dict):
        return {"hit": 0, "miss": 0, "ratio": None}
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    hit_raw = usage.get("prompt_cache_hit_tokens")
    miss_raw = usage.get("prompt_cache_miss_tokens")
    cached_raw = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
    # Only treat the upstream as cache-aware when it actually reported a cache
    # field; a bare prompt count is not evidence about caching either way.
    if hit_raw is None and miss_raw is None and cached_raw is None:
        return {"hit": 0, "miss": 0, "ratio": None}
    hit = hit_raw if isinstance(hit_raw, int) else (cached_raw if isinstance(cached_raw, int) else 0)
    if not isinstance(hit, int):
        hit = 0
    miss = miss_raw if isinstance(miss_raw, int) else max(0, _as_int(input_tokens) - hit)
    if hit == 0 and miss == 0:
        return {"hit": 0, "miss": 0, "ratio": None}
    total = hit + miss
    return {"hit": hit, "miss": miss, "ratio": hit / total if total else None}


def normalize_usage(usage: Any, *, estimated_input_tokens: int | None = None) -> Json | None:
    """Normalize upstream usage for the client.

    estimated_input_tokens substitutes the proxy's estimate for an upstream
    input_tokens: 0 (so the client compacts correctly) and surfaces it as
    estimatedInputTokens; the provider's own numbers are otherwise untouched.
    """
    if not isinstance(usage, dict):
        return None
    input_tokens = _as_int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
    output_tokens = _as_int(usage.get("completion_tokens", usage.get("output_tokens", 0)))
    normalized: Json = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": _as_int(usage.get("total_tokens", input_tokens + output_tokens)),
    }
    if estimated_input_tokens is not None:
        normalized["input_tokens"] = estimated_input_tokens
        normalized["total_tokens"] = estimated_input_tokens + output_tokens
        normalized["estimatedInputTokens"] = estimated_input_tokens
    # Surf the upstream's own prefix-cache accounting back to Codex in the
    # standard Responses shape so the app's token display shows cache hits,
    # and keep the reasoning split when the provider reports one.
    cache = cache_stats_from_usage(usage)
    if cache["hit"] or cache["ratio"] is not None:
        normalized["input_tokens_details"] = {"cached_tokens": cache["hit"]}
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if isinstance(reasoning, int):
        normalized["output_tokens_details"] = {"reasoning_tokens": reasoning}
    return normalized


def _first_choice(chat: Json) -> Json:
    choices = chat.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return {}
