"""Codex app tool snapshot and merge (threads, automations, app navigation).

The Codex app ships most of its toolset as ``type: "namespace"`` entries that
chat-completions upstreams do not understand; the proxy flattens them to plain
functions named ``<namespace>__<tool>``. This module snapshots the app's own
namespaces (``codex_app``, ``plugin_management``) from the installed codex
binary and merges the snapshot into request tool lists that arrive without
those tools, so a routed model can still call ``codex_app__create_thread``,
``codex_app__list_threads``, and friends.

The snapshot comes from ``codex debug prompt-input`` (the same binary
resolution as the native-model capture). The rendered input does not always
carry tool definitions (the current build renders messages only), so a capture
that finds nothing returns None and merges fall back to the checked-in
contrib/codex-app-tools.json snapshot.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from typing import Any

from .meter import state_dir

Json = dict[str, Any]

STATE_TOOLS_NAME = "codex-app-tools.json"

# The two app-native namespaces the app sends in session tool lists (verified
# against live captures); everything else in the rendered input is left alone.
CAPTURED_NAMESPACES = ("codex_app", "plugin_management")

PROMPT_INPUT_TIMEOUT_SEC = 30
VERSION_TIMEOUT_SEC = 10

_REPO_CONTRIB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "contrib",
    STATE_TOOLS_NAME,
)

# (path, mtime_ns, tools) so a per-request merge does not re-read an unchanged
# snapshot; None until the first successful load.
_snapshot_cache: tuple[str, int, list[Json]] | None = None


def state_tools_path() -> str:
    """State-dir snapshot path for the codex_app tool definitions."""
    return os.path.join(state_dir(), STATE_TOOLS_NAME)


def _normalize_tool(flat_name: str, description: Any, parameters: Any) -> Json:
    """One codex_app function entry -> the chat function shape the proxy emits."""
    desc = description if isinstance(description, str) else ""
    params = parameters if isinstance(parameters, dict) else {"type": "object", "properties": {}}
    return {"type": "function", "function": {"name": flat_name, "description": desc, "parameters": params}}


def _collect_codex_app_tools(node: Any, collected: dict[str, Json]) -> None:
    """Walk any rendered prompt-input structure, keeping codex_app function entries.

    Accepts the shapes the app can render: flat names ("codex_app__create_thread"),
    a namespace field, or function entries nested under a namespace group.
    Keyed by flattened name so the same tool found twice is captured once.
    """
    if isinstance(node, list):
        for entry in node:
            _collect_codex_app_tools(entry, collected)
        return
    if not isinstance(node, dict):
        return

    node_type = node.get("type")
    if node_type == "namespace":
        namespace = node.get("name")
        if isinstance(namespace, str) and namespace in CAPTURED_NAMESPACES:
            for sub in node.get("tools") or []:
                if isinstance(sub, dict) and sub.get("type") == "function":
                    sub_name = sub.get("name")
                    if isinstance(sub_name, str) and sub_name:
                        flat = f"{namespace}__{sub_name}"
                        collected[flat] = _normalize_tool(
                            flat, sub.get("description"), sub.get("inputSchema") or sub.get("parameters")
                        )
        return
    if node_type == "function":
        name = node.get("name")
        if isinstance(name, str) and name:
            namespace = node.get("namespace")
            flat = None
            if namespace in CAPTURED_NAMESPACES:
                flat = f"{namespace}__{name}"
            elif namespace is None and "__" in name:
                prefix, _, bare = name.partition("__")
                if prefix in CAPTURED_NAMESPACES and bare:
                    flat = name
            if flat is not None:
                collected[flat] = _normalize_tool(
                    flat, node.get("description"), node.get("inputSchema") or node.get("parameters")
                )
        return
    for value in node.values():
        _collect_codex_app_tools(value, collected)


def _codex_version(binary: str) -> str | None:
    """The binary's version banner for captured_with; None when it cannot be read."""
    try:
        completed = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT_SEC, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


CODEX_BIN_ENV = "OPENCODE_GO_PROXY_CODEX_BIN"
CODEX_FALLBACK_PATHS = (
    os.path.join(os.path.expanduser("~"), ".codex", "packages", "standalone", "current", "bin", "codex"),
    "/Applications/ChatGPT.app/Contents/Resources/codex",
)


def _resolve_codex_bin() -> str | None:
    """Codex CLI path: env override, then standalone install, then app bundle."""
    env = os.environ.get(CODEX_BIN_ENV)
    if env:
        return env
    for candidate in CODEX_FALLBACK_PATHS:
        if os.path.exists(candidate):
            return candidate
    return None


def capture_codex_app_tools() -> list[Json] | None:
    """Snapshot codex_app tool definitions from ``codex debug prompt-input``.

    Returns the normalized chat function list (also written to
    <state-dir>/codex-app-tools.json with captured_at / captured_with), or None
    when the binary is missing, the command fails, or the rendered input
    carries no codex_app tools. Capture failure is never fatal: merges fall
    back to the last snapshot or the checked-in contrib file.
    """
    binary = _resolve_codex_bin()
    try:
        completed = subprocess.run(
            [binary, "debug", "prompt-input"],
            capture_output=True,
            text=True,
            timeout=PROMPT_INPUT_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    try:
        rendered = json.loads(completed.stdout)
    except (ValueError, TypeError):
        return None

    collected: dict[str, Json] = {}
    _collect_codex_app_tools(rendered, collected)
    if not collected:
        return None
    tools = [collected[key] for key in sorted(collected)]
    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "captured_with": _codex_version(binary) or binary,
        "tools": tools,
    }
    path = state_tools_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, indent=2)
    except OSError:
        # The state dir is not writable; the in-memory result still serves this
        # process and later merges fall back to the checked-in contrib file.
        return tools
    global _snapshot_cache
    _snapshot_cache = (path, os.stat(path).st_mtime_ns, list(tools))
    return tools


def load_snapshot_tools() -> list[Json]:
    """The codex_app tool list: state-dir capture first, then the contrib fallback."""
    global _snapshot_cache
    path = state_tools_path()
    if os.path.exists(path):
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            mtime = -1
        if _snapshot_cache is not None and _snapshot_cache[0] == path and _snapshot_cache[1] == mtime:
            return list(_snapshot_cache[2])
        try:
            with open(path, encoding="utf-8") as fh:
                snapshot = json.load(fh)
            tools = [t for t in snapshot.get("tools", []) if isinstance(t, dict)]
        except (OSError, ValueError, TypeError):
            tools = []
        if tools:
            _snapshot_cache = (path, mtime, list(tools))
            return list(tools)

    if os.path.exists(_REPO_CONTRIB_PATH):
        try:
            with open(_REPO_CONTRIB_PATH, encoding="utf-8") as fh:
                snapshot = json.load(fh)
            return [t for t in snapshot.get("tools", []) if isinstance(t, dict)]
        except (OSError, ValueError, TypeError):
            return []
    return []


def _entry_name(tool: Json) -> str | None:
    """The tool's name from either the Responses shape or the chat shape."""
    name = tool.get("name")
    if isinstance(name, str) and name:
        return name
    function = tool.get("function")
    if isinstance(function, dict):
        name = function.get("name")
        if isinstance(name, str) and name:
            return name
    return None


def _existing_flat_names(tools: list[Json]) -> set[str]:
    """Every flattened ``<namespace>__<tool>`` name already present in the request."""
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "namespace":
            namespace = tool.get("name")
            if not isinstance(namespace, str):
                continue
            for sub in tool.get("tools") or []:
                if isinstance(sub, dict):
                    sub_name = _entry_name(sub)
                    if sub_name:
                        names.add(f"{namespace}__{sub_name}")
            continue
        name = _entry_name(tool)
        if name and "__" in name:
            names.add(name)
    return names


def merge_codex_app_tools(tools: list[Json]) -> list[Json]:
    """Append snapshot codex_app tools missing from a request tool list.

    Tools already present under their flattened name (either as a plain
    ``codex_app__<name>`` function or inside a namespace group) are never
    duplicated, and non-codex_app entries are left untouched. A request that
    carries no codex_app tools at all gets the full snapshot appended.
    """
    if not isinstance(tools, list):
        return tools
    existing = _existing_flat_names(tools)
    merged = list(tools)
    for tool in load_snapshot_tools():
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if not isinstance(name, str) or name in existing:
            continue
        merged.append(tool)
        existing.add(name)
    return merged
