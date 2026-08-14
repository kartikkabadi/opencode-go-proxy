"""Unit tests for the shared upstream client (upstream.py)."""

import io
import json
import os
import urllib.error
from http import HTTPStatus
from unittest import mock

import pytest

from opencode_go_proxy.app import ProxyConfig
from opencode_go_proxy.errors import ProxyError
from opencode_go_proxy.secrets import clear_api_key_cache
from opencode_go_proxy.upstream import (
    DEFAULT_CAPTION_TIMEOUT_SEC,
    DEFAULT_MAX_RETRIES,
    call_upstream_chat,
    call_upstream_chat_verbatim,
    caption_timeout_sec,
)


def make_config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1", port=8787, chat_base_url="https://up.test/v1",
        api_key_env="OPENCODE_GO_API_KEY", timeout_sec=10, max_body_bytes=1024 * 1024,
    )


def chat_payload() -> dict:
    return {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]}


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = {"retry-after": retry_after} if retry_after else {}
    return urllib.error.HTTPError(
        "https://up.test/v1/chat/completions", code, "err", hdrs, io.BytesIO(b'{"error":"down"}')
    )


def ok_response() -> mock.Mock:
    raw = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode("utf-8")
    return mock.Mock(status=200, headers={}, read=lambda: raw, __enter__=lambda s: s, __exit__=lambda *a: False)


@pytest.fixture(autouse=True)
def _clear_secret_cache() -> None:
    clear_api_key_cache()
    yield
    clear_api_key_cache()


class TestRetryPolicy:
    def test_retries_on_429_then_succeeds(self) -> None:
        calls = []

        def fake_urlopen(req, **kw):
            calls.append(req)
            if len(calls) == 1:
                raise http_error(429, retry_after="2")
            return ok_response()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("opencode_go_proxy.upstream.time.sleep"):
            _value, retries = call_upstream_chat(chat_payload(), make_config(), "req")
        assert retries == 1
        assert len(calls) == 2

    def test_no_retry_on_permanent_error(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", side_effect=http_error(400)) as urlopen, pytest.raises(ProxyError):
            call_upstream_chat(chat_payload(), make_config(), "req")
        assert urlopen.call_count == 1

    def test_max_retries_zero_disables_retry(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", side_effect=http_error(503)) as urlopen, \
             pytest.raises(ProxyError) as ctx:
            call_upstream_chat(chat_payload(), make_config(), "req", max_retries=0)
        assert urlopen.call_count == 1
        assert ctx.value.status == HTTPStatus.SERVICE_UNAVAILABLE

    def test_network_error_surfaces_502(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")) as urlopen, \
             pytest.raises(ProxyError) as ctx:
            call_upstream_chat(chat_payload(), make_config(), "req", max_retries=0)
        assert urlopen.call_count == 1
        assert ctx.value.status == HTTPStatus.BAD_GATEWAY

    def test_timeout_propagates_to_urlopen(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", return_value=ok_response()) as urlopen:
            call_upstream_chat(chat_payload(), make_config(), "req", timeout_sec=3.5)
        _, kwargs = urlopen.call_args
        assert kwargs.get("timeout") == 3.5

    def test_config_timeout_default(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", return_value=ok_response()) as urlopen:
            call_upstream_chat(chat_payload(), make_config(), "req")
        _, kwargs = urlopen.call_args
        assert kwargs.get("timeout") == 10  # make_config timeout_sec


class TestCaptionBudget:
    def test_defaults(self) -> None:
        assert DEFAULT_CAPTION_TIMEOUT_SEC == 30.0
        assert DEFAULT_MAX_RETRIES == 2

    def test_env_override(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_TIMEOUT_SEC": "45"}, clear=True):
            assert caption_timeout_sec() == 45.0

    def test_malformed_falls_back(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_TIMEOUT_SEC": "x"}, clear=True):
            assert caption_timeout_sec() == 30.0


class TestVerbatimPassthroughClient:
    def test_returns_upstream_status_and_raw_body(self) -> None:
        raw = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode("utf-8")
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", return_value=ok_response()) as urlopen:
            status, body, retries, _content_type, _retry_after = call_upstream_chat_verbatim(chat_payload(), make_config(), "req")

        assert status == 200
        assert body == raw
        assert retries == 0
        urlopen.assert_called_once()

    def test_retries_transient_then_relays_final_error_verbatim(self) -> None:
        calls = []

        def fake_urlopen(req, **kw):
            calls.append(req)
            raise http_error(429)

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("opencode_go_proxy.upstream.time.sleep"):
            status, body, retries, _content_type, _retry_after = call_upstream_chat_verbatim(chat_payload(), make_config(), "req")

        assert status == 429
        assert body == b'{"error":"down"}'
        assert retries == DEFAULT_MAX_RETRIES
        assert len(calls) == DEFAULT_MAX_RETRIES + 1

    def test_permanent_error_relayed_without_retry(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", side_effect=http_error(400)) as urlopen:
            status, body, retries, _content_type, _retry_after = call_upstream_chat_verbatim(chat_payload(), make_config(), "req")

        assert status == 400
        assert body == b'{"error":"down"}'
        assert retries == 0
        urlopen.assert_called_once()

    def test_network_error_still_raises_proxy_error(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")), \
             pytest.raises(ProxyError) as ctx:
            call_upstream_chat_verbatim(chat_payload(), make_config(), "req", max_retries=0)

        assert ctx.value.status == HTTPStatus.BAD_GATEWAY
