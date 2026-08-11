"""Env-matrix coverage for every proxy config knob, present and absent."""
import os
import urllib.error
from unittest import mock

import pytest

from opencode_go_proxy import meter, secrets, upstream, vision
from opencode_go_proxy.app import build_parser
from opencode_go_proxy.config import ProxyConfig, resolve_chat_base_url
from opencode_go_proxy.errors import ProxyError


class TestEnvMatrix:
    @pytest.mark.parametrize("value,expected", [("7", 7), ("0", 0), ("", 2)])
    def test_max_retries(self, value, expected) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_MAX_RETRIES": value}, clear=True):
            assert upstream.default_max_retries() == expected

    @pytest.mark.parametrize("value,expected", [("5", 5.0), ("0.5", 1.0), ("junk", 30.0), ("", 30.0)])
    def test_caption_timeout(self, value, expected) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_TIMEOUT_SEC": value}, clear=True):
            assert upstream.caption_timeout_sec() == expected

    @pytest.mark.parametrize(
        "value,expected", [("mimo-v2.5", "mimo-v2.5"), ("auto", "deepseek-v4-flash"), ("", "deepseek-v4-flash")]
    )
    def test_caption_model(self, value, expected) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_MODEL": value}, clear=True):
            assert vision.resolve_caption_model("deepseek-v4-flash") == expected

    @pytest.mark.parametrize("value,expected", [("low", "low"), ("high", "high"), ("none", None), ("off", None)])
    def test_caption_detail(self, value, expected) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_DETAIL": value}, clear=True):
            assert vision.caption_detail() == expected

    def test_state_dir(self, tmp_path) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_STATE_DIR": str(tmp_path)}, clear=True):
            assert meter.state_dir() == str(tmp_path)
        with mock.patch.dict(os.environ, {}, clear=True):
            assert meter.state_dir() == os.path.join(os.path.expanduser("~"), ".codex", "opencode-go-proxy")

    def test_key_env(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_API_KEY_ENV": "CUSTOM_KEY_VAR"}, clear=True):
            assert secrets.configured_key_env() == "CUSTOM_KEY_VAR"
        with mock.patch.dict(os.environ, {}, clear=True):
            assert secrets.configured_key_env() == "OPENCODE_GO_API_KEY"

    def test_estimate_zero_input_kill_switch(self) -> None:
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_ESTIMATE_ZERO_INPUT": "0"}, clear=True):
            assert meter.estimate_input_tokens("m", 1000, usage) is None
        with mock.patch.dict(os.environ, {}, clear=True):
            assert meter.estimate_input_tokens("m", 1000, usage) >= 1000

    def test_user_agent_header(self) -> None:
        captured: dict = {}

        def fake_urlopen(request, **kw):
            captured["ua"] = request.get_header("User-agent")
            raise urllib.error.URLError("stop")

        cfg = ProxyConfig(bind="127.0.0.1", port=1, chat_base_url="https://example.test/v1", api_key_env="OPENCODE_GO_API_KEY", timeout_sec=5, max_body_bytes=1048576)
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_GO_PROXY_USER_AGENT": "custom-ua/1", "OPENCODE_GO_PROXY_MAX_RETRIES": "0"},
            clear=True,
        ), mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), mock.patch(
            "opencode_go_proxy.upstream.resolve_api_key", return_value="k"
        ):
            try:
                upstream.call_upstream_chat({"model": "x", "messages": []}, cfg, "req")
            except ProxyError:
                pass
        assert captured.get("ua") == "custom-ua/1"

    @pytest.mark.parametrize("name", ["OPENCODE_GO_BASE_URL", "OPENCODE_ZEN_BASE_URL"])
    def test_base_url_override(self, name) -> None:
        with mock.patch.dict(os.environ, {name: "https://example.test/v1"}, clear=True):
            assert resolve_chat_base_url() == "https://example.test/v1"

    def test_base_url_precedence(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_GO_BASE_URL": "https://a.test/v1", "OPENCODE_ZEN_BASE_URL": "https://b.test/v1"},
            clear=True,
        ):
            assert resolve_chat_base_url() == "https://a.test/v1"

    def test_parser_env_defaults(self) -> None:
        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_PROXY_PORT": "8801", "OPENCODE_GO_PROXY_BIND": "127.0.0.1"}, clear=True
        ):
            args = build_parser().parse_args([])
            assert args.port == 8801
            assert args.bind == "127.0.0.1"
