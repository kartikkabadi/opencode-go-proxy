"""One secret store seam: env vars then macOS keychain, cached per process.

Resolution order is fixed: the configured env var, then ``OPENCODE_API_KEY``,
then the macOS keychain services (``opencode-go-api-key``,
``codex-router-opencode-go``, plus the ``CODEX_KEYCHAIN_SERVICE`` override).
"""

from __future__ import annotations

import os
import subprocess
import threading
from http import HTTPStatus

from .config import ProxyConfig
from .errors import ProxyError
from .trace import trace

_api_key_cache: str | None = None
_api_key_lock = threading.Lock()

# Keychain services that may hold the OpenCode Go API key. The proxy's own
# install uses opencode-go-api-key; the codex-router install on the same
# machine stores it under codex-router-opencode-go. Trying both keeps the
# proxy working regardless of which harness provisioned the credential.
_KEYCHAIN_SERVICES: tuple[str, ...] = ("opencode-go-api-key", "codex-router-opencode-go")


def keychain_services() -> list[str]:
    services: list[str] = []
    service_env = os.environ.get("CODEX_KEYCHAIN_SERVICE")
    if service_env:
        services.append(service_env)
    services.extend(_KEYCHAIN_SERVICES)
    return list(dict.fromkeys(services))


def configured_key_env() -> str:
    return os.environ.get("OPENCODE_GO_PROXY_API_KEY_ENV", "OPENCODE_GO_API_KEY")


def clear_api_key_cache() -> None:
    global _api_key_cache
    _api_key_cache = None


def resolve_api_key(config: ProxyConfig, request_id: str) -> str:
    global _api_key_cache
    if _api_key_cache:
        return _api_key_cache

    with _api_key_lock:
        if _api_key_cache:
            return _api_key_cache

        # OPENCODE_API_KEY is the standard OpenCode env var; accept it as a
        # fallback so the proxy works in environments that provisioned the
        # key under the generic name.
        for env in (config.api_key_env, "OPENCODE_API_KEY"):
            if not env:
                continue
            api_key = os.environ.get(env)
            if api_key:
                _api_key_cache = api_key
                trace("credential.source", request_id=request_id, source="env", env=env)
                return api_key

        for keychain_service in keychain_services():
            trace("credential.lookup", request_id=request_id, source="keychain", service=keychain_service)
            try:
                completed = subprocess.run(
                    ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", keychain_service, "-w"],
                    check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=10,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                completed = None
            if completed and completed.returncode == 0:
                first_line = completed.stdout.splitlines()[0].strip() if completed.stdout.splitlines() else ""
                if first_line:
                    _api_key_cache = first_line
                    trace("credential.source", request_id=request_id, source="keychain", service=keychain_service)
                    return first_line

        raise ProxyError(
            HTTPStatus.UNAUTHORIZED,
            f"missing API key: set ${config.api_key_env} or $OPENCODE_API_KEY or keychain:{_KEYCHAIN_SERVICES[0]}",
        )
