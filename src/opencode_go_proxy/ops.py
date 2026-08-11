"""Operational commands: doctor, smoke-test, support-bundle, install, status.

Stdlib-only and single-provider like the rest of the proxy. `doctor` inspects
env/keychain, /health, the port, the meter, logs, the catalog, config, and the
upstream; `--fix` repairs only what is safe without writing config.toml (log
and meter directories, catalog render). `smoke-test` sends one marker prompt
through the local proxy and asserts the marker comes back. `support-bundle`
writes the JSON schema v1 diagnostic bundle with secret-shaped values
redacted. `install` copies the launchd plist and loads the agent (macOS only,
requires --yes); `status` reports running/port/log paths. Uninstall, update,
and rollback are documented, not implemented, because they touch the live
agent and stay approval-gated.
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

from . import __version__, secrets
from .config import ProxyConfig, resolve_chat_base_url
from .meter import usage_events_path
from .protocol import DEFAULT_MODEL

Json = dict[str, Any]

LOG_DIR = os.path.join(os.path.expanduser("~"), ".codex", "logs")
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
HEALTH_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/health"

# The proxy base smoke-test targets; point it at an isolated scratch proxy
# (for example port 8790) when the live 8787 agent must not be touched.
PROXY_BASE_URL_ENV = "OPENCODE_GO_PROXY_BASE_URL"
DEFAULT_PROXY_BASE_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"
SMOKE_MARKER = "OPENCODE_GO_SMOKE_OK"

LAUNCHD_LABEL = "com.opencode-go.proxy"
LAUNCH_AGENTS_DIR_ENV = "OPENCODE_GO_PROXY_LAUNCH_AGENTS_DIR"
PLIST_SOURCE_ENV = "OPENCODE_GO_PROXY_PLIST_SOURCE"

_LOG_NAMES = ("opencode-go-proxy.log", "opencode-go-proxy.err")

# Secret-looking keys redacted from the support bundle; everything else is
# left verbatim so a config dump stays readable. The key name is any run of
# word characters ending in a secret suffix (so OPENCODE_API_KEY and
# env.api_key match, not just a bare api_key), bounded by a non-word char
# before it, and the value may be single- or double-quoted TOML.
_SECRET_KEY = re.compile(
    r'(?i)(?<![\w])([A-Za-z0-9_]*?(?:api[_-]?key|apikey|token|secret|password|authorization|bearer|access[_-]?key))\s*=\s*(?:"[^"]*"|\'[^\']*\')'
)
_SECRET_NAME = re.compile(r"(?i)(?:api[_-]?key|apikey|token|secret|password|authorization|bearer|access[_-]?key)")
_SECRET_VALUE = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+|(\bsk-[A-Za-z0-9_\-]{8,}\b)|(-----BEGIN [A-Z ]*PRIVATE KEY-----)")


class Check:
    """One doctor check: ok/warn/fail status, a detail line, and an optional fix."""

    __slots__ = ("detail", "fix", "name", "status")

    def __init__(self, name: str, status: str, detail: str = "", fix: str | None = None) -> None:
        self.name = name
        self.status = status
        self.detail = detail
        self.fix = fix

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail, "fix": self.fix}


def _proxy_config() -> ProxyConfig:
    """A ProxyConfig with the defaults doctor/status use."""
    return ProxyConfig(
        bind=DEFAULT_HOST,
        port=DEFAULT_PORT,
        chat_base_url=resolve_chat_base_url(),
        api_key_env=secrets.configured_key_env(),
        timeout_sec=180,
        max_body_bytes=20 * 1024 * 1024,
    )


def check_api_key() -> Check:
    """The proxy key resolves from env or the macOS keychain (never printed)."""
    source = secrets.api_key_source(_proxy_config())
    if source:
        return Check("api key", "ok", f"resolved from {source}")
    return Check(
        "api key",
        "fail",
        f"no key in ${secrets.configured_key_env()}, $OPENCODE_API_KEY, or keychain",
        fix=f"Set ${secrets.configured_key_env()} or add the key to keychain:{secrets.keychain_services()[0]}.",
    )


def check_config_file() -> Check:
    """config.toml exists at the path the proxy serves."""
    if os.path.exists(CONFIG_PATH):
        return Check("config file", "ok", CONFIG_PATH)
    return Check("config file", "fail", f"{CONFIG_PATH} missing", fix="Start Codex once so it writes config.toml, then rerun doctor.")


def check_config() -> Check:
    """Codex config points openai_base_url at the proxy."""
    if not os.path.exists(CONFIG_PATH):
        return Check("config", "fail", f"{CONFIG_PATH} missing", fix="See the config file check.")
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        return Check("config", "fail", str(exc))
    if f"{DEFAULT_HOST}:{DEFAULT_PORT}" in text:
        return Check("config", "ok", f"openai_base_url points at {DEFAULT_HOST}:{DEFAULT_PORT}")
    return Check("config", "fail", f"openai_base_url does not point at {DEFAULT_HOST}:{DEFAULT_PORT}", fix="Run the config-manager enable step (plan 012) once it ships.")


def _catalog_models() -> tuple[str, list[Json]]:
    """Return (path, models) for the catalog the proxy serves, seed as fallback."""
    from . import catalog

    path = catalog.default_catalog_path()
    models: list[Json] = []
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        raw_models = raw.get("models")
        if isinstance(raw_models, list):
            models = [m for m in raw_models if isinstance(m, dict)]
    except (OSError, json.JSONDecodeError):
        seed = catalog.load_seed_compact()
        if seed is not None:
            path = catalog.seed_compact_path() or path
            raw_models = seed.get("models")
            if isinstance(raw_models, list):
                models = [m for m in raw_models if isinstance(m, dict)]
    return path, models


def check_catalog() -> Check:
    """The runtime catalog is readable and lists models."""
    path, models = _catalog_models()
    if not models:
        return Check("catalog", "fail", f"no models readable from {path}", fix="Run `opencode-go-proxy doctor --fix` (renders the seed) or `--refresh-catalog`.")
    return Check("catalog", "ok", f"{len(models)} model(s) in {path}")


def check_port(url: str = HEALTH_URL) -> Check:
    """The proxy port is free, or owned by this proxy rather than another process."""
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            if resp.status == 200:
                return Check("port", "ok", f"owned by proxy on {url}")
            return Check("port", "warn", f"listener on {url} answered HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        return Check("port", "warn", f"listener on {url} answered HTTP {exc.code}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Check("port", "ok", f"free; proxy not running ({exc})")


def check_service(url: str = HEALTH_URL) -> Check:
    """The local proxy's /health endpoint answers 200."""
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return Check("service", "ok", f"health {resp.status} on {url}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Check("service", "fail", f"not reachable on {url}: {exc}", fix="Start the proxy (install/status), then rerun doctor.")


def check_meter() -> Check:
    """The usage meter is writable at its configured location; probe never records."""
    path = usage_events_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
    except OSError:
        return Check("meter", "fail", "cannot write usage-events.jsonl", fix=f"Make {os.path.dirname(path)} writable, then rerun doctor.")
    return Check("meter", "ok", path)


def check_logs() -> Check:
    """The log directory is writable; report any existing proxy log files."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        probe = os.path.join(LOG_DIR, f".doctor-probe-{os.getpid()}")
        with open(probe, "w", encoding="utf-8"):
            pass
        os.unlink(probe)
    except OSError as exc:
        return Check("logs", "fail", f"log dir not writable: {LOG_DIR} ({exc})", fix=f"Make {LOG_DIR} writable, then rerun doctor.")
    present = [n for n in _LOG_NAMES if os.path.exists(os.path.join(LOG_DIR, n))]
    detail = ", ".join(present) if present else "writable; no log files yet"
    return Check("logs", "ok", detail)


def check_upstream() -> Check:
    """Best-effort reachability of the chat base URL host (warn, never fails doctor)."""
    base = resolve_chat_base_url()
    try:
        with urllib.request.urlopen(base, timeout=5):
            return Check("upstream", "ok", f"{base} reachable")
    except urllib.error.HTTPError as exc:
        return Check("upstream", "ok", f"{base} reachable (HTTP {exc.code})")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return Check("upstream", "warn", f"{base} not reachable ({exc})", fix="Check DNS/network; smoke-test exercises the full path.")


def _run_checks() -> list[Check]:
    return [
        check_api_key(),
        check_config_file(),
        check_config(),
        check_catalog(),
        check_port(),
        check_service(),
        check_meter(),
        check_logs(),
        check_upstream(),
    ]


def _doctor_report(checks: list[Check]) -> Json:
    return {"ok": not any(c.status == "fail" for c in checks), "checks": [c.to_json() for c in checks]}


def _safe_fixes() -> None:
    """Apply --fix repairs that never write config.toml; failures stay hints."""
    for check in _run_checks():
        if check.name == "logs" and not check.ok:
            try:
                os.makedirs(LOG_DIR, exist_ok=True)
            except OSError:
                pass
        elif check.name == "meter" and not check.ok:
            try:
                os.makedirs(os.path.dirname(usage_events_path()), exist_ok=True)
            except OSError:
                pass
        elif check.name == "catalog" and not check.ok:
            try:
                from . import catalog

                catalog.prepare_runtime_catalog()
            except Exception as exc:  # noqa: BLE001 - repair is best-effort
                from .trace import trace

                trace("doctor.fix.skipped", check="catalog", error=str(exc))


def doctor(argv: list[str] | None = None) -> int:
    """Run the check set; --json for machine output, --fix for safe repairs."""
    argv = argv or []
    json_output = "--json" in argv
    if "--fix" in argv:
        _safe_fixes()
        if not json_output:
            sys.stdout.write("Repair completed; verifying the result.\n\n")
    checks = _run_checks()
    report = _doctor_report(checks)
    if json_output:
        sys.stdout.write(json.dumps({"ok": report["ok"], "checks": report["checks"], "version": __version__}, indent=2) + "\n")
    else:
        for c in checks:
            sys.stdout.write(f"{c.status.upper():5} {c.name}: {c.detail}\n")
            if c.status == "fail" and c.fix:
                sys.stdout.write(f"      Fix: {c.fix}\n")
        sys.stdout.write("\n" + ("OK" if report["ok"] else "FAIL") + "\n")
    return 0 if report["ok"] else 1


def _proxy_base_url(argv: list[str]) -> str:
    base = os.environ.get(PROXY_BASE_URL_ENV) or DEFAULT_PROXY_BASE_URL
    if "--base-url" in argv:
        i = argv.index("--base-url")
        if i + 1 < len(argv):
            base = argv[i + 1]
    return base.rstrip("/")


def _response_text(payload: Json) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    values: list[str] = []
    for item in payload.get("output") or []:
        for part in item.get("content") or []:
            if isinstance(part.get("text"), str):
                values.append(part["text"])
    return "\n".join(values)


def smoke_test(argv: list[str] | None = None) -> int:
    """Send one marker prompt through the local proxy; non-zero on failure."""
    argv = argv or []
    base = _proxy_base_url(argv)
    marker = SMOKE_MARKER
    payload = json.dumps({
        "model": DEFAULT_MODEL,
        "input": f"Reply with exactly {marker} and nothing else.",
        "stream": False,
    }, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/v1/responses",
        data=payload,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            value = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:400].strip()
        sys.stdout.write(f"smoke-test FAIL: proxy answered HTTP {exc.code}: {body or 'no body'}\n")
        return 1
    except (urllib.error.URLError, OSError, ValueError) as exc:
        sys.stdout.write(f"smoke-test FAIL: {base} not reachable: {exc}\n")
        return 1
    text = _response_text(value)
    if marker in text:
        sys.stdout.write(f"smoke-test OK: model={value.get('model', DEFAULT_MODEL)} marker={marker} received\n")
        return 0
    sys.stdout.write(f"smoke-test FAIL: completed response missing marker {marker!r}; got {text[:200]!r}\n")
    return 1


def _mask_secret_values(text: str) -> str:
    """Redact secret-looking values from a config dump for a support bundle."""
    return _SECRET_KEY.sub(lambda m: f"{m.group(1)}= ***redacted***", text)


def _redact_text(text: str) -> str:
    """Redact secret-shaped values plus bearer/sk-/private-key patterns in log text."""
    return _SECRET_VALUE.sub(lambda m: m.group(1) + "[REDACTED]" if m.group(1) else "[REDACTED]", _mask_secret_values(text))


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


def _env_summary() -> Json:
    """Proxy-relevant env vars; secret-shaped names and values are redacted."""
    names = sorted(
        name for name in os.environ
        if name.startswith("OPENCODE")
        or name in {"CODEX_KEYCHAIN_SERVICE", "CODEX_MODEL_CATALOG", "CHAT_COMPLETIONS_BASE_URL"}
    )
    summary: Json = {}
    for name in names:
        value = os.environ[name]
        if _SECRET_NAME.search(name) or _SECRET_VALUE.search(value):
            summary[name] = "[REDACTED]"
        else:
            summary[name] = value
    return summary


def _config_snapshot() -> Json:
    if not os.path.exists(CONFIG_PATH):
        return {"path": CONFIG_PATH, "exists": False}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            text = f.read()
        return {"path": CONFIG_PATH, "exists": True, "redacted": _mask_secret_values(text)}
    except OSError as exc:
        return {"path": CONFIG_PATH, "exists": True, "error": str(exc)}


def _meter_tail(path: str, limit: int = 200) -> list[Json]:
    if not os.path.exists(path):
        return []
    events: list[Json] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return events
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _log_tail(path: str, limit: int = 200) -> str | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None
    return _redact_text("".join(lines[-limit:]))


def _git_commit() -> str | None:
    try:
        completed = subprocess.run(["git", "-C", _repo_root(), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def support_bundle(argv: list[str] | None = None) -> int:
    """Write the JSON schema v1 diagnostic bundle to a mode-600 file."""
    argv = argv or []
    out: str | None = None
    if "--output" in argv:
        i = argv.index("--output")
        if i + 1 < len(argv):
            out = argv[i + 1]
    if not out:
        out = os.path.join(os.getcwd(), f"opencode-go-support-{time.strftime('%Y%m%dT%H%M%S')}.json")

    _ensure_meter()
    checks = _run_checks()
    catalog_path, catalog_models = _catalog_models()
    bundle: Json = {
        "schemaVersion": 1,
        "generatedAt": datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z"),
        "version": __version__,
        "privacy": "Credential values are redacted; redacted log tails may still contain prompts or responses.",
        "runtime": {
            "platform": sys.platform,
            "release": platform.release(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "gitCommit": _git_commit(),
        },
        "env": _env_summary(),
        "config": _config_snapshot(),
        "meter": {"path": usage_events_path(), "tail": _meter_tail(usage_events_path())},
        "logs": {"dir": LOG_DIR, "tail": {name: _log_tail(os.path.join(LOG_DIR, name)) for name in _LOG_NAMES}},
        "catalog": {"path": catalog_path, "modelCount": len(catalog_models)},
        "doctor": _doctor_report(checks),
    }
    serialized = json.dumps(bundle, indent=2) + "\n"
    with open(out, "w", encoding="utf-8") as f:
        f.write(serialized)
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    sys.stdout.write(out + "\n")
    return 0


def _launch_agents_dir() -> str:
    return os.environ.get(LAUNCH_AGENTS_DIR_ENV) or os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")


def _plist_source() -> str:
    env = os.environ.get(PLIST_SOURCE_ENV)
    if env:
        return env
    return os.path.join(_repo_root(), "contrib", "launchd", "com.opencode-go.proxy.plist")


def _launchd_loaded() -> bool:
    try:
        completed = subprocess.run(["launchctl", "list", LAUNCHD_LABEL], capture_output=True, text=True, timeout=10, check=False)
        return completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def install(argv: list[str] | None = None) -> int:
    """Install the launchd agent (macOS only). Requires --yes; loads via launchctl."""
    argv = argv or []
    if platform.system() != "Darwin":
        sys.stdout.write("install is macOS-only (launchd); Linux uses contrib/systemd/opencode-go-proxy.service.\n")
        return 1
    if "--yes" not in argv:
        sys.stdout.write("install would copy the launchd plist into ~/Library/LaunchAgents and load the agent; re-run with --yes to confirm.\n")
        return 2
    source = _plist_source()
    if not os.path.exists(source):
        sys.stdout.write(f"install FAIL: plist not found at {source} (set {PLIST_SOURCE_ENV} to point at one)\n")
        return 1
    agents_dir = _launch_agents_dir()
    try:
        os.makedirs(agents_dir, exist_ok=True)
        target = os.path.join(agents_dir, os.path.basename(source))
        shutil.copy2(source, target)
        os.chmod(target, 0o644)
    except OSError as exc:
        sys.stdout.write(f"install FAIL: could not write {target}: {exc}\n")
        return 1
    if _launchd_loaded():
        sys.stdout.write(f"install OK: {target} copied; agent already loaded\n")
        return 0
    completed = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", target], capture_output=True, text=True, timeout=30, check=False)
    if completed.returncode == 0:
        sys.stdout.write(f"install OK: {target} copied and loaded\n")
        return 0
    sys.stdout.write(f"install FAIL: launchctl bootstrap exited {completed.returncode}: {(completed.stderr or completed.stdout or '').strip()}\n")
    return 1


def status(argv: list[str] | None = None) -> int:
    """Report whether the proxy is running, on which port, and where logs live."""
    argv = argv or []
    service = check_service()
    port = check_port()
    log_files = [n for n in _LOG_NAMES if os.path.exists(os.path.join(LOG_DIR, n))]
    loaded = _launchd_loaded()
    state: Json = {
        "running": service.ok,
        "port": DEFAULT_PORT,
        "service": service.detail,
        "portState": port.detail,
        "launchd": {"label": LAUNCHD_LABEL, "loaded": loaded},
        "logs": {"dir": LOG_DIR, "files": log_files},
    }
    if "--json" in argv:
        sys.stdout.write(json.dumps(state, indent=2) + "\n")
    else:
        sys.stdout.write(f"proxy: {'running' if service.ok else 'stopped'} ({service.detail})\n")
        sys.stdout.write(f"port: {port.detail}\n")
        sys.stdout.write(f"launchd: {'loaded' if loaded else 'not loaded'} ({LAUNCHD_LABEL})\n")
        sys.stdout.write(f"logs: {LOG_DIR}\n")
        for name in log_files:
            sys.stdout.write(f"  {name}\n")
    return 0
