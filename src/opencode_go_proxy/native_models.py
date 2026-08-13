"""Native Codex model capture: snapshot `codex debug models` to the state dir.

The native catalog (official GPT models plus whatever else the installed Codex
CLI serves) is captured once per state dir. Routing, the merged catalog, and
the native passthrough meter all read this snapshot.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
from typing import Any

from .meter import state_dir
from .trace import trace

Json = dict[str, Any]

CODEX_BIN_ENV = "OPENCODE_GO_PROXY_CODEX_BIN"
NATIVE_CATALOG_NAME = "native-models.json"
NATIVE_CAPTURE_TIMEOUT_SEC = 30

# The exact keys routing and the merged catalog consume; anything else the CLI
# prints is not projected into the snapshot.
KEPT_KEYS = (
    "slug",
    "display_name",
    "description",
    "default_reasoning_level",
    "supported_reasoning_levels",
    "multi_agent_version",
    "context_window",
)


class NativeCaptureError(Exception):
    """Raised when the Codex CLI cannot be found, run, or parsed."""


def native_models_path() -> str:
    return os.path.join(state_dir(), NATIVE_CATALOG_NAME)


def resolve_codex_bin() -> str:
    """Codex CLI path: env override, then standalone install, then app bundle."""
    env = os.environ.get(CODEX_BIN_ENV)
    if env:
        return env
    candidates = (
        os.path.join(
            os.path.expanduser("~"), ".codex", "packages", "standalone", "current", "bin", "codex"
        ),
        "/Applications/ChatGPT.app/Contents/Resources/codex",
    )
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    raise NativeCaptureError(f"codex CLI not found; set {CODEX_BIN_ENV} to its path")


def _run(bin_path: str, args: list[str]) -> str:
    try:
        completed = subprocess.run(
            [bin_path, *args], capture_output=True, text=True, timeout=NATIVE_CAPTURE_TIMEOUT_SEC
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NativeCaptureError(f"failed to run {bin_path}: {exc}") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise NativeCaptureError(
            f"{bin_path} {' '.join(args)} exited {completed.returncode}"
            + (f": {stderr}" if stderr else "")
        )
    return completed.stdout


def codex_version(bin_path: str) -> str | None:
    """Best-effort `codex --version` line; None when the call fails."""
    try:
        line = _run(bin_path, ["--version"]).splitlines()[0].strip()
    except (NativeCaptureError, IndexError):
        return None
    return line or None


def _project_model(entry: Json) -> Json:
    return {key: entry[key] for key in KEPT_KEYS if key in entry}


def _parse_models(raw: str) -> list[Json]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise NativeCaptureError(f"codex debug models returned invalid JSON: {exc}") from exc
    if isinstance(payload, list):
        models = payload
    elif isinstance(payload, dict) and isinstance(payload.get("models"), list):
        models = payload["models"]
    else:
        raise NativeCaptureError("codex debug models returned an unexpected shape")
    return [entry for entry in models if isinstance(entry, dict)]


def _exclude_opencode_go_slugs(slugs: set[str]) -> set[str]:
    """Drop every slug that belongs to the opencode-go catalog, prefixed or not."""
    from opencode_go_proxy import catalog as _catalog  # safe: catalog never imports this module

    known = set(_catalog.load_known_slugs())
    return {s for s in slugs if "/" not in s and s not in known}


def _native_only(models: list[Json]) -> list[Json]:
    """Drop provider-prefixed and opencode-go slugs from the capture.

    ``codex debug models`` renders the configured catalog, so it can contain
    ``opencode-go/<slug>`` entries (and bare opencode-go slugs once the merged
    catalog is configured). Those must never count as native: routing would
    hijack opencode-go models to the ChatGPT backend.
    """
    captured = {str(m["slug"]) for m in models if isinstance(m.get("slug"), str) and m["slug"]}
    allowed = _exclude_opencode_go_slugs(captured)
    return [m for m in models if m.get("slug") in allowed]


def capture_native_models(*, dry_run: bool = False) -> Json:
    """Run `codex debug models`, project to the kept keys, and snapshot it.

    dry_run skips the state-dir write. Returns the snapshot shape
    (captured_at, captured_with, models) in both cases.
    """
    bin_path = resolve_codex_bin()
    snapshot = {
        "captured_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "captured_with": codex_version(bin_path),
        "models": [_project_model(entry) for entry in _native_only(_parse_models(_run(bin_path, ["debug", "models"])))],
    }
    if not dry_run:
        path = native_models_path()
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".native-models-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(snapshot, handle, indent=2)
                handle.write("\n")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError as exc:
                    trace("native_capture.cleanup_failed", path=tmp, error=str(exc))
    return snapshot


def load_native_capture(path: str | None = None) -> Json:
    """Read the native snapshot; an empty models list when missing or malformed."""
    if path is None:
        path = native_models_path()
    try:
        with open(path) as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"captured_at": None, "captured_with": None, "models": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return {"captured_at": None, "captured_with": None, "models": []}
    return {
        "captured_at": payload.get("captured_at"),
        "captured_with": payload.get("captured_with"),
        "models": [entry for entry in payload["models"] if isinstance(entry, dict)],
    }


def native_slugs(capture: Json) -> set[str]:
    # Same exclusion as _native_only so consumers of a stale snapshot file
    # (routing) can never treat opencode-go slugs as native.
    return _exclude_opencode_go_slugs(
        {str(entry["slug"]) for entry in capture.get("models", []) if entry.get("slug")}
    )

def native_effort_vocabulary(capture: Json) -> set[str]:
    """Union of reasoning efforts declared across the native models."""
    efforts: set[str] = set()
    for entry in capture.get("models", []):
        for level in entry.get("supported_reasoning_levels") or []:
            if isinstance(level, dict) and level.get("effort"):
                efforts.add(str(level["effort"]))
    return efforts


def native_capture_cmd(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="opencode-go-proxy native-capture")
    parser.add_argument("--dry-run", action="store_true", help="print the capture without writing it")
    args = parser.parse_args(argv)
    try:
        snapshot = capture_native_models(dry_run=args.dry_run)
    except NativeCaptureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    efforts = ", ".join(sorted(native_effort_vocabulary(snapshot)))
    count = len(snapshot["models"])
    if args.dry_run:
        print(f"{count} native models; efforts: {efforts}")
    else:
        print(f"captured {count} native models to {native_models_path()}")
        if efforts:
            print(f"efforts: {efforts}")
    return 0
