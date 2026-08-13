"""Reliability layer: upstream retry with backoff + honest usage meter."""

import io
import json
import os
import shutil
import tempfile
import urllib.error
from http import HTTPStatus
from unittest import mock

import pytest

from opencode_go_proxy.app import (
    ProxyConfig,
    ProxyError,
    call_upstream_chat,
    handle_responses_request,
    handle_streaming_request,
)
from opencode_go_proxy.meter import usage_events_path


def make_config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1", port=8787, chat_base_url="https://up.test/v1",
        api_key_env="OPENCODE_GO_API_KEY", timeout_sec=10, max_body_bytes=1024 * 1024,
    )


def chat_payload() -> dict:
    return {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]}


def ok_response(body: dict) -> object:
    raw = json.dumps(body).encode("utf-8")
    return mock.Mock(status=200, headers={}, read=lambda: raw, __enter__=lambda s: s, __exit__=lambda *a: False)


def http_error(code: int, body: str = "", retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = {"retry-after": retry_after} if retry_after else {}
    return urllib.error.HTTPError(
        "https://up.test/v1/chat/completions", code, "err", hdrs, io.BytesIO(body.encode("utf-8"))
    )


def ok_chat() -> dict:
    return {
        "id": "chatcmpl-1", "object": "chat.completion", "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


@pytest.fixture
def env():
    state = tempfile.mkdtemp(prefix="ogg-rel-")
    with mock.patch.dict(os.environ, {
        "OPENCODE_GO_API_KEY": "test-key",
        "OPENCODE_GO_PROXY_STATE_DIR": state,
    }):
        yield state
    shutil.rmtree(state, ignore_errors=True)


class TestRetry:
    def test_retries_on_429_then_succeeds(self, env) -> None:
        cfg = make_config()
        calls = []
        def fake_urlopen(req, **kw):
            calls.append(req)
            if len(calls) == 1:
                raise http_error(429, retry_after="2")
            return ok_response(ok_chat())
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("opencode_go_proxy.app.time.sleep"):
            value, retries = call_upstream_chat(chat_payload(), cfg, "req1")
        assert retries == 1
        assert value["choices"][0]["message"]["content"] == "hi"
        assert len(calls) == 2

    def test_retries_on_5xx_then_succeeds(self, env) -> None:
        cfg = make_config()
        calls = []
        def fake_urlopen(req, **kw):
            calls.append(req)
            if len(calls) <= 1:
                raise http_error(503)
            return ok_response(ok_chat())
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("opencode_go_proxy.app.time.sleep"):
            _value, retries = call_upstream_chat(chat_payload(), cfg, "req2")
        assert retries == 1
        assert len(calls) == 2

    def test_retries_on_network_error(self, env) -> None:
        cfg = make_config()
        calls = []
        def fake_urlopen(req, **kw):
            calls.append(req)
            if len(calls) == 1:
                raise urllib.error.URLError("connection refused")
            return ok_response(ok_chat())
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("opencode_go_proxy.app.time.sleep"):
            _value, retries = call_upstream_chat(chat_payload(), cfg, "req3")
        assert retries == 1
        assert len(calls) == 2

    def test_gives_up_after_max_retries(self, env) -> None:
        cfg = make_config()
        urlopen = mock.Mock(side_effect=http_error(429))
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_MAX_RETRIES": "2"}), \
             mock.patch("urllib.request.urlopen", urlopen), \
             mock.patch("opencode_go_proxy.app.time.sleep"), pytest.raises(ProxyError) as ei:
            call_upstream_chat(chat_payload(), cfg, "req4")
        assert ei.value.status == HTTPStatus.TOO_MANY_REQUESTS
        assert urlopen.call_count == 3  # initial + 2 retries

    def test_no_retry_on_permanent_error(self, env) -> None:
        cfg = make_config()
        calls = []
        def fake_urlopen(req, **kw):
            calls.append(req)
            raise http_error(400)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), pytest.raises(ProxyError):
            call_upstream_chat(chat_payload(), cfg, "req5")
        assert len(calls) == 1  # no retry on 400

    def test_disabled_retries(self, env) -> None:
        cfg = make_config()
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_MAX_RETRIES": "0"}), \
             mock.patch("urllib.request.urlopen", side_effect=http_error(429)), pytest.raises(ProxyError):
            call_upstream_chat(chat_payload(), cfg, "req6")


class TestMeterThroughHandler:
    def test_success_records_usage_event(self, env) -> None:
        cfg = make_config()
        payload = {"model": "deepseek-v4-flash", "input": "hi", "stream": False}
        with mock.patch("urllib.request.urlopen", return_value=ok_response(ok_chat())):
            handle_responses_request(payload, cfg, "req7")
        with open(usage_events_path(), encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        assert len(events) == 1
        e = events[0]
        assert e["status"] == 200
        assert e["input_tokens"] == 3
        assert e["total_tokens"] == 4
        assert "streamAborted" not in e

    def _events(self) -> list[dict]:
        with open(usage_events_path(), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_streaming_429_exhausted_records_429_not_aborted(self, env) -> None:
        # A connect-phase 429 exhaustion must meter the real upstream status,
        # not a synthetic 502 + streamAborted (nothing was ever streamed).
        cfg = make_config()
        payload = {"model": "deepseek-v4-flash", "input": "hi", "stream": True}
        urlopen = mock.Mock(side_effect=http_error(429, retry_after="5"))
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_MAX_RETRIES": "1"}), \
             mock.patch("urllib.request.urlopen", urlopen), \
             mock.patch("opencode_go_proxy.app.time.sleep"):
            handle_streaming_request(payload, cfg, "req8", io.BytesIO())
        assert urlopen.call_count == 2  # initial + 1 retry
        events = self._events()
        assert len(events) == 1
        e = events[0]
        assert e["status"] == 429
        assert e["retries"] == 1
        assert "streamAborted" not in e

    def test_failed_turn_after_retry_records_retries(self, env) -> None:
        # A non-streaming turn that exhausts retries must still report how many
        # retries it burned (regression: ProxyError branch omitted retries).
        cfg = make_config()
        payload = {"model": "deepseek-v4-flash", "input": "hi", "stream": False}
        urlopen = mock.Mock(side_effect=http_error(429))
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_MAX_RETRIES": "1"}), \
             mock.patch("urllib.request.urlopen", urlopen), \
             mock.patch("opencode_go_proxy.app.time.sleep"), pytest.raises(ProxyError):
            handle_responses_request(payload, cfg, "req9")
        events = self._events()
        assert len(events) == 1
        e = events[0]
        assert e["status"] == 429
        assert e["retries"] == 1
