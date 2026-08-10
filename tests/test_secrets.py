"""Unit tests for the shared secret store (secrets.py)."""

import os
import subprocess
from http import HTTPStatus
from unittest import mock

from opencode_go_proxy.app import ProxyConfig
from opencode_go_proxy.errors import ProxyError
from opencode_go_proxy.secrets import clear_api_key_cache, keychain_services, resolve_api_key


def make_config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=8787,
        chat_base_url="https://opencode.ai/zen/go/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=1,
        max_body_bytes=20 * 1024 * 1024,
    )


def _keychain_result(service: str, returncode: int = 0, stdout: str = "key\n") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["security", "find-generic-password", "-s", service, "-w"], returncode, stdout=stdout, stderr="")


class TestResolutionOrder:
    def test_configured_env_wins(self) -> None:
        clear_api_key_cache()
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "env-key"}, clear=True), mock.patch(
            "opencode_go_proxy.secrets.subprocess.run"
        ) as run:
            assert resolve_api_key(make_config(), "req") == "env-key"
        run.assert_not_called()

    def test_generic_env_fallback(self) -> None:
        clear_api_key_cache()
        with mock.patch.dict(os.environ, {"OPENCODE_API_KEY": "std-key"}, clear=True), mock.patch(
            "opencode_go_proxy.secrets.subprocess.run"
        ) as run:
            assert resolve_api_key(make_config(), "req") == "std-key"
        run.assert_not_called()

    def test_keychain_fallback_order(self) -> None:
        clear_api_key_cache()
        services: list[str] = []

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            service = args[args.index("-s") + 1]
            services.append(service)
            if service == "codex-router-opencode-go":
                return _keychain_result(service, stdout="router-key\n")
            return _keychain_result(service, returncode=1, stdout="")

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "opencode_go_proxy.secrets.subprocess.run", side_effect=fake_run
        ):
            assert resolve_api_key(make_config(), "req") == "router-key"
        assert services == ["opencode-go-api-key", "codex-router-opencode-go"]

    def test_custom_keychain_service_override_first(self) -> None:
        clear_api_key_cache()
        services: list[str] = []

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            service = args[args.index("-s") + 1]
            services.append(service)
            return _keychain_result(service, stdout="custom-key\n")

        with mock.patch.dict(os.environ, {"CODEX_KEYCHAIN_SERVICE": "my-service"}, clear=True), mock.patch(
            "opencode_go_proxy.secrets.subprocess.run", side_effect=fake_run
        ):
            assert resolve_api_key(make_config(), "req") == "custom-key"
        assert services[0] == "my-service"

    def test_missing_key_raises_401(self) -> None:
        clear_api_key_cache()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "opencode_go_proxy.secrets.subprocess.run", return_value=_keychain_result("opencode-go-api-key", returncode=1)
        ), mock.patch("opencode_go_proxy.secrets.subprocess.run", side_effect=None) as run:
            run.side_effect = [_keychain_result(s, returncode=1) for s in keychain_services()]
            try:
                resolve_api_key(make_config(), "req")
                raise AssertionError("expected ProxyError")
            except ProxyError as exc:
                assert exc.status == HTTPStatus.UNAUTHORIZED
                assert "$OPENCODE_GO_API_KEY" in exc.message
                assert "keychain" in exc.message


class TestCache:
    def test_caches_after_first_resolution(self) -> None:
        clear_api_key_cache()
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "env-key"}, clear=True):
            assert resolve_api_key(make_config(), "req1") == "env-key"
            # Second call with env cleared must still return the cached value
            # without touching env or keychain.
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
                "opencode_go_proxy.secrets.subprocess.run"
            ) as run:
                assert resolve_api_key(make_config(), "req2") == "env-key"
                run.assert_not_called()

    def test_clear_resets_cache(self) -> None:
        clear_api_key_cache()
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "env-key"}, clear=True):
            resolve_api_key(make_config(), "req")
        clear_api_key_cache()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "opencode_go_proxy.secrets.subprocess.run", return_value=_keychain_result("opencode-go-api-key", stdout="kc\n")
        ):
            assert resolve_api_key(make_config(), "req") == "kc"


class TestKeychainServices:
    def test_defaults(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert keychain_services() == ["opencode-go-api-key", "codex-router-opencode-go"]

    def test_override_is_deduplicated(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_KEYCHAIN_SERVICE": "opencode-go-api-key"}, clear=True):
            assert keychain_services() == ["opencode-go-api-key", "codex-router-opencode-go"]
