"""Operational commands: doctor, smoke-test, support-bundle.

Stdlib-only and single-provider like the rest of the proxy. `doctor` is
read-only: it inspects env/keychain, /health, the meter, logs, and config, and
records nothing. `smoke-test` sends one real chat-completion to upstream;
`support-bundle` writes a tarball that redacts secret values.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from typing import Any

from . import __version__
from .meter import usage_events_path

Json = dict[str, Any]

LOG_DIR = os.path.join(os.path.expanduser("~"), ".codex", "logs")
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
DEFAULT_PORT = 8787
HEALTH_URL = f"http://127.0.0.1:{DEFAULT_PORT}/health"
CHAT_BASE_URL = os.environ.get("CHAT_COMPLETIONS_BASE_URL", "https://opencode.ai/zen/go/v1")

_LOG_NAMES = ("opencode-go-proxy.log", "opencode-go-proxy.err")

# Secret-looking keys redacted from the support bundle; everything else is
# left verbatim so a config dump stays readable. The key name is any run of
# word characters ending in a secret suffix (so OPENCODE_API_KEY and
# env.api_key match, not just a bare api_key), bounded by a non-word char
# before it, and the value may be single- or double-quoted TOML.
_SECRET_KEY = re.compile(
    r'(?i)(?<![\w])([A-Za-z0-9_]*?(?:api[_-]?key|apikey|token|secret|password|authorization|bearer|access[_-]?key))\s*=\s*(?:"[^"]*"|\'[^\']*\')'
)


class Check:
    """One doctor check: a name, whether it passed, and an optional fix hint."""

    __slots__ = ("hint", "name", "ok")

    def __init__(self, name: str, ok: bool, hint: str | None = None) -> None:
        self.name = name
        self.ok = ok
        self.hint = hint

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "hint": self.hint}


def _configured_key_env() -> str:
    return os.environ.get("OPENCODE_GO_PROXY_API_KEY_ENV", "OPENCODE_GO_API_KEY")


# Keychain services that may hold the API key. Mirrors app.resolve_api_key's
# order so doctor reports the same resolution the proxy actually uses: the
# configured env var, then $OPENCODE_API_KEY, then the macOS keychain.
_KEYCHAIN_SERVICES: tuple[str, ...] = ("opencode-go-api-key", "codex-router-opencode-go")


def _keychain_services() -> list[str]:
    services: list[str] = []
    service_env = os.environ.get("CODEX_KEYCHAIN_SERVICE")
    if service_env:
        services.append(service_env)
    services.extend(_KEYCHAIN_SERVICES)
    return list(dict.fromkeys(services))


def _resolve_api_key() -> tuple[str, str] | None:
    """Resolve the API key the way the proxy does; return (value, source) or None.

    Never prints the value; callers use it only for a presence check.
    """
    for env in (_configured_key_env(), "OPENCODE_API_KEY"):
        if not env:
            continue
        value = os.environ.get(env)
        if value:
            return value, f"${env}"
    for keychain_service in _keychain_services():
        try:
            completed = subprocess.run(
                ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", keychain_service, "-w"],
                check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if completed.returncode == 0:
            first_line = completed.stdout.splitlines()[0].strip() if completed.stdout.splitlines() else ""
            if first_line:
                return first_line, f"keychain:{keychain_service}"
    return None


def check_api_key() -> Check:
    """The proxy key is present in env or the macOS keychain (never printed)."""
    resolved = _resolve_api_key()
    if resolved is not None:
        return Check("api key", True, f"resolved from {resolved[1]}")
    return Check("api key", False, f"set ${_configured_key_env()}, $OPENCODE_API_KEY, or keychain:{_KEYCHAIN_SERVICES[0]}")


def check_service(url: str = HEALTH_URL) -> Check:
    """The local proxy's /health endpoint answers 200."""
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return Check("service", resp.status == 200, f"health {resp.status}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Check("service", False, f"not reachable: {exc}")


def check_meter() -> Check:
    """The usage meter is writable at its configured location; probe never records."""
    path = usage_events_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Probe writability without recording: an append-mode open creates the
        # file if missing but adds no content, so the honest meter stays clean.
        with open(path, "a", encoding="utf-8"):
            pass
    except OSError:
        return Check("meter", False, "cannot write usage-events.jsonl")
    return Check("meter", True, path)


def check_logs() -> Check:
    """At least one proxy log file exists."""
    present = [n for n in _LOG_NAMES if os.path.exists(os.path.join(LOG_DIR, n))]
    if present:
        return Check("logs", True, ", ".join(present))
    return Check("logs", False, f"no proxy logs in {LOG_DIR}")


def check_config() -> Check:
    """Codex config points openai_base_url at the proxy."""
    if not os.path.exists(CONFIG_PATH):
        return Check("config", False, f"{CONFIG_PATH} missing")
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        return Check("config", False, str(exc))
    if f"127.0.0.1:{DEFAULT_PORT}" in text:
        return Check("config", True, "openai_base_url points at proxy")
    return Check("config", False, f"openai_base_url does not point at 127.0.0.1:{DEFAULT_PORT}")


def doctor(argv: list[str] | None = None) -> int:
    argv = argv or []
    checks = [
        check_api_key(),
        check_service(),
        check_meter(),
        check_logs(),
        check_config(),
    ]
    ok = all(c.ok for c in checks)
    if "--json" in argv:
        sys.stdout.write(json.dumps({"ok": ok, "checks": [c.to_json() for c in checks], "version": __version__}, indent=2) + "\n")
    else:
        for c in checks:
            sys.stdout.write(("ok  " if c.ok else "FAIL ") + c.name + "\n")
            if not c.ok and c.hint:
                sys.stdout.write("     " + c.hint + "\n")
        sys.stdout.write("\n" + ("OK" if ok else "FAIL") + "\n")
    return 0 if ok else 1


def smoke_test() -> int:
    """Send one tiny real chat-completion to upstream; non-zero on failure."""
    if not check_api_key().ok:
        return 1
    payload = json.dumps({
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{CHAT_BASE_URL}/chat/completions",
        data=payload,
        headers={
            "authorization": f"Bearer {os.environ.get(_configured_key_env(), '')}",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            value = json.loads(resp.read().decode("utf-8"))
            content = (value.get("choices") or [{}])[0].get("message", {}).get("content", "")
            sys.stdout.write(f"smoke-test OK: model={value.get('model')} content={content!r}\n")
            return 0
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
        sys.stdout.write(f"smoke-test FAIL: {exc}\n")
        return 1


def _mask_secret_values(text: str) -> str:
    """Redact secret-looking values from a config dump for a support bundle."""
    return _SECRET_KEY.sub(lambda m: f"{m.group(1)}= ***redacted***", text)


def _ensure_meter() -> None:
    """Make sure the meter file exists so the bundle always shows where it lives."""
    path = usage_events_path()
    if not os.path.exists(path):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8"):
                pass
        except OSError:
            pass


def support_bundle(argv: list[str] | None = None) -> int:
    """Write a tarball of logs, meter, version, and redacted config to a file."""
    argv = argv or []
    out: str | None = None
    if "--output" in argv:
        i = argv.index("--output")
        if i + 1 < len(argv):
            out = argv[i + 1]
    if not out:
        out = os.path.join(os.getcwd(), f"opencode-go-support-{time.strftime('%Y%m%dT%H%M%S')}.tar.gz")

    _ensure_meter()
    members: dict[str, str | bytes] = {}
    members["opencode-go/version.json"] = json.dumps({"version": __version__}, indent=2).encode("utf-8")
    for name in _LOG_NAMES:
        p = os.path.join(LOG_DIR, name)
        if os.path.exists(p):
            members[f"opencode-go/{name}"] = p
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                members["opencode-go/config.toml"] = _mask_secret_values(f.read()).encode("utf-8")
        except OSError:
            pass
    mp = usage_events_path()
    if os.path.exists(mp):
        members["opencode-go/usage-events.jsonl"] = mp

    with tarfile.open(out, "w:gz") as tf:
        for arcname, src in members.items():
            if isinstance(src, bytes):
                info = tarfile.TarInfo(arcname)
                info.size = len(src)
                tf.addfile(info, io.BytesIO(src))
            else:
                tf.add(src, arcname=arcname)
    sys.stdout.write(out + "\n")
    return 0
