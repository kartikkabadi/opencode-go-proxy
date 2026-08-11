import os
from unittest import mock

from opencode_go_proxy.config import (
    DEFAULT_CHAT_BASE_URL,
    ProxyConfig,
    resolve_chat_base_url,
)


class TestBaseUrlResolution:
    def test_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert resolve_chat_base_url() == DEFAULT_CHAT_BASE_URL

    def test_opencode_go_wins(self) -> None:
        with mock.patch.dict(os.environ, {
            "OPENCODE_GO_BASE_URL": "https://go.example/v1",
            "OPENCODE_ZEN_BASE_URL": "https://zen.example/v1",
            "CHAT_COMPLETIONS_BASE_URL": "https://legacy.example/v1",
        }):
            assert resolve_chat_base_url() == "https://go.example/v1"

    def test_zen_fallback(self) -> None:
        with mock.patch.dict(os.environ, {
            "OPENCODE_ZEN_BASE_URL": "https://zen.example/v1/",
            "CHAT_COMPLETIONS_BASE_URL": "https://legacy.example/v1",
        }):
            assert resolve_chat_base_url() == "https://zen.example/v1"

    def test_legacy_env_last(self) -> None:
        with mock.patch.dict(os.environ, {"CHAT_COMPLETIONS_BASE_URL": "https://legacy.example/v1"}):
            assert resolve_chat_base_url() == "https://legacy.example/v1"

    def test_explicit_beats_env(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_BASE_URL": "https://go.example/v1"}):
            assert resolve_chat_base_url("https://cli.example/v1") == "https://cli.example/v1"

    def test_parser_default_honors_env(self) -> None:
        from opencode_go_proxy.app import build_parser

        with mock.patch.dict(os.environ, {"OPENCODE_GO_BASE_URL": "https://go.example/v1"}):
            args = build_parser().parse_args([])
        assert args.chat_base_url == "https://go.example/v1"

    def test_proxy_config_uses_resolution(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_ZEN_BASE_URL": "https://zen.example/v1"}):
            config = ProxyConfig(
                bind="127.0.0.1",
                port=8787,
                chat_base_url=resolve_chat_base_url(),
                api_key_env="OPENCODE_GO_API_KEY",
                timeout_sec=180,
                max_body_bytes=20 * 1024 * 1024,
            )
        assert config.chat_base_url == "https://zen.example/v1"
