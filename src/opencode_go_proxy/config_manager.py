"""Managed ~/.codex/config.toml block: enable, disable, status.

The proxy owns exactly one marker-commented block in the root of Codex
config.toml:

    # BEGIN opencode-go-proxy-managed
    openai_base_url = "http://127.0.0.1:8787/v1"
    model_catalog_json = "~/.codex/opencode-go-proxy/opencode-go-catalog.json"
    # END opencode-go-proxy-managed

Semantics mirror codex-router's config-manager: enable never replaces a
user-owned openai_base_url or model_catalog_json, disable removes only the
managed block (and the file when the block was its only content), and the
Codex Voice realtime keys are added only when the user has not set them, so
Voice keeps talking to Codex's native endpoints.

Tests must target a temp config via OPENCODE_GO_PROXY_CONFIG_PATH; this
module never touches the real ~/.codex/config.toml on its own.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile

from .catalog import STATE_CATALOG_NAME
from .meter import state_dir

CONFIG_PATH_ENV = "OPENCODE_GO_PROXY_CONFIG_PATH"
DEFAULT_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")

MANAGED_BASE_URL_ENV = "OPENCODE_GO_PROXY_MANAGED_BASE_URL"
DEFAULT_BASE_URL = "http://127.0.0.1:8787/v1"

MANAGED_CATALOG_ENV = "OPENCODE_GO_PROXY_MANAGED_CATALOG"

START_MARKER = "# BEGIN opencode-go-proxy-managed"
END_MARKER = "# END opencode-go-proxy-managed"

REALTIME_CALL_KEY = "experimental_realtime_webrtc_call_base_url"
REALTIME_WS_KEY = "experimental_realtime_ws_base_url"
DEFAULT_CHATGPT_BASE_URL = "https://chatgpt.com/backend-api"
DEFAULT_REALTIME_WS_BASE_URL = "https://api.openai.com/v1"


class ConfigError(Exception):
    """A managed config.toml change was refused or could not be parsed."""


def config_path() -> str:
    """The config.toml the manager operates on (env override for tests)."""
    return os.environ.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH


def managed_base_url() -> str:
    """The proxy base URL written into the managed block."""
    return os.environ.get(MANAGED_BASE_URL_ENV) or DEFAULT_BASE_URL


def managed_catalog_path() -> str:
    """The state-dir catalog the managed block points Codex at."""
    override = os.environ.get(MANAGED_CATALOG_ENV)
    if override:
        return override
    return os.path.join(state_dir(), STATE_CATALOG_NAME)


def native_realtime_call_base_url(contents: str) -> str:
    """Codex Voice's WebRTC endpoint on native, mirroring codex-router."""
    root_lines, _tables = _split_root(contents)
    base = (_root_value(root_lines, "chatgpt_base_url") or DEFAULT_CHATGPT_BASE_URL).rstrip("/")
    return base if base.endswith("/codex") else f"{base}/codex"


def _split_root(contents: str) -> tuple[list[str], list[str]]:
    """Root lines (before the first [table]) and everything from there on."""
    lines = contents.split("\n")
    for index, line in enumerate(lines):
        if re.match(r"^\s*\[", line):
            return lines[:index], lines[index:]
    return lines, []


def _trim_blank_edges(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _assignment_value(line: str) -> str:
    """Best-effort TOML scalar value: basic string, literal string, or raw."""
    raw = line.split("=", 1)[1].strip() if "=" in line else line.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, str):
                return parsed
        except json.JSONDecodeError:
            pass
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    return raw.strip("\"'")


def _root_value(lines: list[str], key: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for line in lines:
        if pattern.match(line):
            return _assignment_value(line)
    return None


def _root_has_value(lines: list[str], key: str) -> bool:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    return any(pattern.match(line) for line in lines)


def _toml_string(value: str) -> str:
    """JSON escaping is valid TOML basic-string escaping (incl. Windows paths)."""
    return json.dumps(value)


def _block_lines(base_url: str, catalog_path: str, root_lines: list[str]) -> list[str]:
    # A root assignment that already matches is preserved, not duplicated:
    # repeating the key inside the block would produce invalid TOML.
    lines = [START_MARKER]
    if _root_value(root_lines, "openai_base_url") != base_url:
        lines.append(f"openai_base_url = {_toml_string(base_url)}")
    if _root_value(root_lines, "model_catalog_json") != catalog_path:
        lines.append(f"model_catalog_json = {_toml_string(catalog_path)}")
    if not _root_has_value(root_lines, REALTIME_CALL_KEY):
        call_url = native_realtime_call_base_url("\n".join(root_lines))
        lines.append(f"{REALTIME_CALL_KEY} = {_toml_string(call_url)}")
    if not _root_has_value(root_lines, REALTIME_WS_KEY):
        lines.append(f"{REALTIME_WS_KEY} = {_toml_string(DEFAULT_REALTIME_WS_BASE_URL)}")
    lines.append(END_MARKER)
    return lines


def _render(root_lines: list[str], table_lines: list[str]) -> str:
    root = _trim_blank_edges(root_lines)
    tables = _trim_blank_edges(table_lines)
    if not root:
        return ("\n".join(tables) + "\n") if tables else ""
    parts = [*root, ""]
    if tables:
        parts.extend(tables)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _block_bounds(contents: str) -> tuple[int, int] | None:
    """(start, end) line indexes of the managed block, or None when absent."""
    lines = contents.split("\n")
    starts = [i for i, line in enumerate(lines) if line.strip() == START_MARKER]
    ends = [i for i, line in enumerate(lines) if line.strip() == END_MARKER]
    if len(starts) > 1:
        raise ConfigError(f"config.toml has {len(starts)} '{START_MARKER}' blocks; refusing to guess")
    if not starts:
        if ends:
            raise ConfigError(f"config.toml has a stray '{END_MARKER}' without its opening marker; refusing to guess")
        return None
    if not ends:
        raise ConfigError(f"config.toml has '{START_MARKER}' without '{END_MARKER}'; refusing to guess")
    start, end = starts[0], ends[0]
    if end < start:
        raise ConfigError("config.toml managed block is malformed (END before BEGIN); refusing to guess")
    if start != 0 and lines[start - 1].strip():
        raise ConfigError("config.toml managed block is not on its own lines; refusing to guess")
    return start, end


def _without_block(contents: str) -> str:
    bounds = _block_bounds(contents)
    if bounds is None:
        return contents
    start, end = bounds
    lines = contents.split("\n")
    return "\n".join(lines[:start] + lines[end + 1 :])


def _atomic_write(path: str, contents: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f"{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def enable() -> dict[str, object]:
    """Insert the managed block; refuse to clobber user-owned values."""
    path = config_path()
    contents = ""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                contents = handle.read()
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
    # Validate any existing block up front (multiple/malformed = refuse).
    _block_bounds(contents)
    cleaned = _without_block(contents)
    root_lines, table_lines = _split_root(cleaned)
    base_url = managed_base_url()
    catalog_path = managed_catalog_path()
    existing_base = _root_value(root_lines, "openai_base_url")
    if existing_base and existing_base != base_url:
        raise ConfigError(f"refusing to replace user-owned openai_base_url = {existing_base!r}")
    existing_catalog = _root_value(root_lines, "model_catalog_json")
    if existing_catalog and existing_catalog != catalog_path:
        raise ConfigError(f"refusing to replace user-owned model_catalog_json = {existing_catalog!r}")
    next_root = [*_trim_blank_edges(root_lines), "", *_block_lines(base_url, catalog_path, root_lines)]
    rendered = _render(next_root, table_lines)
    changed = rendered != contents
    if changed:
        _atomic_write(path, rendered)
    return {
        "action": "enable",
        "path": path,
        "changed": changed,
        "openai_base_url": base_url,
        "model_catalog_json": catalog_path,
    }


def disable() -> dict[str, object]:
    """Remove the managed block; delete the file when it was the only content."""
    path = config_path()
    if not os.path.exists(path):
        return {"action": "disable", "path": path, "changed": False, "file_removed": False}
    try:
        with open(path, encoding="utf-8") as handle:
            contents = handle.read()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    bounds = _block_bounds(contents)
    if bounds is None:
        return {"action": "disable", "path": path, "changed": False, "file_removed": False}
    cleaned = _without_block(contents)
    if not cleaned.strip():
        os.unlink(path)
        return {"action": "disable", "path": path, "changed": True, "file_removed": True}
    root_lines, table_lines = _split_root(cleaned)
    rendered = _render(root_lines, table_lines)
    if rendered != contents:
        _atomic_write(path, rendered)
    return {"action": "disable", "path": path, "changed": True, "file_removed": False}


def status() -> dict[str, object]:
    """Managed state: block present, values, and user-owned Voice keys."""
    path = config_path()
    result: dict[str, object] = {"path": path, "exists": os.path.exists(path)}
    contents = ""
    if result["exists"]:
        try:
            with open(path, encoding="utf-8") as handle:
                contents = handle.read()
        except OSError as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
    bounds = _block_bounds(contents)
    root_lines, _tables = _split_root(contents)
    block_present = bounds is not None
    user_owned: list[str] = []
    managed_owned: list[str] = []
    if block_present:
        assert bounds is not None
        start, end = bounds
        block_lines = contents.split("\n")[start + 1 : end]
        for key in (REALTIME_CALL_KEY, REALTIME_WS_KEY):
            if _root_has_value(block_lines, key):
                managed_owned.append(key)
    outside = _without_block(contents)
    outside_root, _ = _split_root(outside)
    for key in (REALTIME_CALL_KEY, REALTIME_WS_KEY):
        if _root_has_value(outside_root, key):
            user_owned.append(key)
    result.update(
        {
            "state": "enabled" if block_present else "disabled",
            "managed": block_present,
            "openai_base_url": _root_value(root_lines, "openai_base_url"),
            "model_catalog_json": _root_value(root_lines, "model_catalog_json"),
            "voice_keys_user_owned": user_owned,
            "voice_keys_managed": managed_owned,
        }
    )
    return result


def config_cmd(argv: list[str] | None = None) -> int:
    """opencode-go-proxy config enable|disable|status [--json]."""
    args = list(argv or [])
    if not args:
        sys.stderr.write("usage: opencode-go-proxy config enable|disable|status [--json]\n")
        return 2
    command = args[0]
    as_json = "--json" in args[1:]
    try:
        if command == "enable":
            report = enable()
            text = "managed block written"
            if not report["changed"]:
                text = "already enabled, no change"
        elif command == "disable":
            report = disable()
            if report["file_removed"]:
                text = f"file removed ({report['path']})"
            elif report["changed"]:
                text = f"managed block removed ({report['path']})"
            else:
                text = "already disabled, no change"
        elif command == "status":
            report = status()
            text = ""
            if not as_json:
                state = "enabled" if report["managed"] else "disabled"
                sys.stdout.write(f"state: {state}\n")
                sys.stdout.write(f"config: {report['path']} ({'present' if report['exists'] else 'missing'})\n")
                sys.stdout.write(f"openai_base_url: {report['openai_base_url'] or '(unset)'}\n")
                sys.stdout.write(f"model_catalog_json: {report['model_catalog_json'] or '(unset)'}\n")
                if report["voice_keys_user_owned"]:
                    sys.stdout.write(f"user voice keys: {', '.join(sorted(report['voice_keys_user_owned']))}\n")
                if report["voice_keys_managed"]:
                    sys.stdout.write(f"managed voice keys: {', '.join(sorted(report['voice_keys_managed']))}\n")
        else:
            sys.stderr.write(f"unknown config subcommand {command!r}; use enable, disable, or status\n")
            return 2
    except ConfigError as exc:
        sys.stderr.write(f"config {command}: {exc}\n")
        return 1
    if as_json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    elif command in {"enable", "disable"}:
        sys.stdout.write(f"config {command}: {text}\n")
    return 0
