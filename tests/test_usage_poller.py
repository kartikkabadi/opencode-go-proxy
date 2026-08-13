"""Usage poller: OpenCode Go plan usage fetch, TTL cache, failure degradation."""

import json
import urllib.error
import urllib.request
from typing import Self
from unittest import mock

import pytest

from opencode_go_proxy import usage_poller
from opencode_go_proxy.config import ProxyConfig
from opencode_go_proxy.secrets import clear_api_key_cache

USAGE_BODY = {
    "usage": {
        "rolling": {"status": "ok", "percent": 26, "resetsAt": "2026-08-14T05:00:00Z"},
        "weekly": {"status": "ok", "percent": 41, "resetsAt": "2026-08-16T00:00:00Z"},
        "monthly": {"status": "ok", "percent": 12, "resetsAt": "2026-08-31T00:00:00Z"},
    }
}


class FakeResponse:
    """Minimal urllib response double: readable, context-managed."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def make_config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=8787,
        chat_base_url="https://opencode.ai/zen/go/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=10,
        max_body_bytes=1024 * 1024,
    )


@pytest.fixture(autouse=True)
def isolated_poller(monkeypatch) -> None:
    """No TTL cache, no API key cache, and never touch the real keychain."""
    usage_poller.clear_cache()
    clear_api_key_cache()
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("OPENCODE_GO_USAGE_URL", raising=False)
    monkeypatch.setattr("opencode_go_proxy.secrets.keychain_services", list)
    yield
    usage_poller.clear_cache()
    clear_api_key_cache()


class TestFetch:
    def test_parses_usage_and_authorizes(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test-usage")
        seen: list[urllib.request.Request] = []

        def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            seen.append(request)
            return FakeResponse(json.dumps(USAGE_BODY).encode())

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            usage = usage_poller.poll_go_usage(make_config())
        assert usage == USAGE_BODY["usage"]
        assert seen[0].get_header("Authorization") == "Bearer sk-test-usage"
        assert seen[0].full_url == usage_poller.DEFAULT_USAGE_URL

    def test_ttl_cache_serves_second_call_without_network(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test-usage")
        calls: list[urllib.request.Request] = []

        def fake_urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
            calls.append(request)
            return FakeResponse(json.dumps(USAGE_BODY).encode())

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            first = usage_poller.poll_go_usage(make_config())
            second = usage_poller.poll_go_usage(make_config())
        assert first == second == USAGE_BODY["usage"]
        assert len(calls) == 1

    def test_http_error_returns_none(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test-usage")
        error = urllib.error.HTTPError(usage_poller.DEFAULT_USAGE_URL, 500, "boom", None, None)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            assert usage_poller.poll_go_usage(make_config()) is None

    def test_network_error_returns_none(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test-usage")
        with mock.patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            assert usage_poller.poll_go_usage(make_config()) is None

    def test_malformed_body_returns_none(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test-usage")
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(b"{not json")):
            assert usage_poller.poll_go_usage(make_config()) is None

    def test_body_without_usage_key_returns_none(self, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_GO_API_KEY", "sk-test-usage")
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(b'{"error": "no usage"}')):
            assert usage_poller.poll_go_usage(make_config()) is None

    def test_missing_api_key_returns_none_without_network(self) -> None:
        with mock.patch("urllib.request.urlopen") as urlopen:
            assert usage_poller.poll_go_usage(make_config()) is None
        urlopen.assert_not_called()
