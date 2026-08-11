"""Plan 007 correctness contract: empty-completion retry, zero-input estimation, keepalive to end."""

import io
import json
import math
import os
import time
from unittest import mock

from opencode_go_proxy.app import ProxyConfig
from opencode_go_proxy.meter import (
    estimate_input_tokens,
    note_real_input_tokens,
    usage_events_path,
)
from opencode_go_proxy.secrets import clear_api_key_cache
from opencode_go_proxy.streaming import handle_streaming_request


def make_config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1", port=8787, chat_base_url="https://up.test/v1",
        api_key_env="OPENCODE_GO_API_KEY", timeout_sec=10, max_body_bytes=1024 * 1024,
    )


class _UpstreamStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)
        self.status = 200
        self.headers = {}

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _StallingStream(_UpstreamStream):
    """Stream that sleeps `stall_sec` before every line after the first."""

    def __init__(self, lines: list[bytes], stall_sec: float) -> None:
        super().__init__(lines)
        self._stall = stall_sec

    def __iter__(self):
        for i, line in enumerate(self._lines):
            if i > 0:
                time.sleep(self._stall)
            yield line


def _empty_chunk(usage: dict | None = None) -> bytes:
    chunk = {"id": "1", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    if usage is not None:
        chunk["usage"] = usage
    return b"data: " + json.dumps(chunk).encode("utf-8") + b"\n"


def _content_chunk(text: str, usage: dict | None = None) -> bytes:
    chunk = {"id": "1", "choices": [{"index": 0, "delta": {"content": text}}]}
    if usage is not None:
        chunk["usage"] = usage
    return b"data: " + json.dumps(chunk).encode("utf-8") + b"\n"


def _stream(urlopen, *, env: dict[str, str] | None = None):
    """Drive handle_streaming_request; returns (events, wfile, meter_events)."""
    clear_api_key_cache()
    merged = {"OPENCODE_GO_API_KEY": "test-key"}
    if env:
        merged.update(env)
    cfg = make_config()
    payload = {"model": "deepseek-v4-flash", "input": "hi", "stream": True}
    wfile = io.BytesIO()
    with mock.patch.dict(os.environ, merged), mock.patch("urllib.request.urlopen", urlopen):
        handle_streaming_request(payload, cfg, "req", wfile)
    events: list[dict] = []
    for block in wfile.getvalue().decode("utf-8").split("\n\n"):
        block = block.strip()
        if not block.startswith("data: "):
            continue
        data = block[len("data: "):]
        if data == "[DONE]":
            continue
        events.append(json.loads(data))
    try:
        with open(usage_events_path(), encoding="utf-8") as f:
            meter = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        meter = []
    return events, wfile, meter


class TestEmptyCompletionRetry:
    def test_empty_stream_retried_once_and_retry_produces_content(self) -> None:
        # First attempt ends with no text/tool/reasoning: terminal events are
        # held and the identical request is re-run once. The retry's content is
        # finalized normally and the meter never marks the turn empty.
        first = [
            _empty_chunk(usage={"prompt_tokens": 3, "completion_tokens": 0, "total_tokens": 3}),
            b"data: [DONE]\n",
        ]
        second = [
            _content_chunk("hi"),
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n',
            b"data: [DONE]\n",
        ]
        calls: list = []

        def fake_urlopen(req, **kw):
            calls.append(req)
            return _UpstreamStream(second if len(calls) == 2 else first)

        events, wfile, meter = _stream(fake_urlopen)
        assert len(calls) == 2
        assert not any(e["type"] == "response.error" for e in events)
        completed = next(e for e in events if e["type"] == "response.completed")["response"]
        assert completed["output_text"] == "hi"
        assert b"[DONE]" in wfile.getvalue()
        assert len(meter) == 1
        assert meter[0]["status"] == 200
        assert meter[0].get("emptyCompletion") is not True
        assert meter[0]["retries"] == 1

    def test_retry_also_empty_emits_empty_completion_error(self) -> None:
        empty = [
            _empty_chunk(usage={"prompt_tokens": 3, "completion_tokens": 0, "total_tokens": 3}),
            b"data: [DONE]\n",
        ]
        calls: list = []

        def fake_urlopen(req, **kw):
            calls.append(req)
            return _UpstreamStream(empty)

        events, wfile, meter = _stream(fake_urlopen)
        assert len(calls) == 2
        errors = [e for e in events if e["type"] == "response.error"]
        assert len(errors) == 1
        assert errors[0]["error"]["code"] == "empty_completion"
        assert "upstream returned an empty completion" in errors[0]["error"]["message"]
        assert not any(e["type"] == "response.completed" for e in events)
        assert b"[DONE]" in wfile.getvalue()
        assert len(meter) == 1
        assert meter[0]["status"] == 200
        assert meter[0]["emptyCompletion"] is True
        assert meter[0]["retries"] == 1

    def test_content_stream_is_not_retried(self) -> None:
        lines = [
            _content_chunk("hi"),
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n',
            b"data: [DONE]\n",
        ]
        calls: list = []

        def fake_urlopen(req, **kw):
            calls.append(req)
            return _UpstreamStream(lines)

        events, _, meter = _stream(fake_urlopen)
        assert len(calls) == 1
        assert any(e["type"] == "response.completed" for e in events)
        assert not any(e["type"] == "response.error" for e in events)
        assert meter[0]["status"] == 200
        assert not meter[0].get("emptyCompletion")

    def test_no_data_upstream_is_not_retried(self) -> None:
        # A 200 that opens but never sends SSE stays the existing 502 path:
        # that is an upstream failure, not an empty completion, so no retry.
        calls: list = []

        def fake_urlopen(req, **kw):
            calls.append(req)
            return _UpstreamStream([])

        events, _, meter = _stream(fake_urlopen)
        assert len(calls) == 1
        assert any(e["type"] == "response.error" for e in events)
        assert meter[0]["status"] == 502
        assert meter[0]["emptyCompletion"] is True


class TestZeroInputEstimation:
    def test_zero_input_is_estimated_in_response_and_meter(self) -> None:
        usage = {"prompt_tokens": 0, "completion_tokens": 5, "total_tokens": 5}
        lines = [
            _content_chunk("hi", usage=usage),
            b"data: [DONE]\n",
        ]
        captured: dict = {}

        def fake_urlopen(req, **kw):
            captured["raw"] = req.data
            return _UpstreamStream(lines)

        events, _, meter = _stream(fake_urlopen)
        completed = next(e for e in events if e["type"] == "response.completed")["response"]
        expected = min(272000, max(1000, math.ceil(len(captured["raw"]) / 3.3)))
        assert completed["usage"]["input_tokens"] == expected
        assert completed["usage"]["estimatedInputTokens"] == expected
        assert completed["usage"]["total_tokens"] == expected + 5
        assert meter[0]["inputTokens"] == 0  # provider's number stays in the meter
        assert meter[0]["estimatedInputTokens"] == expected

    def test_kill_switch_disables_estimation(self) -> None:
        usage = {"prompt_tokens": 0, "completion_tokens": 5, "total_tokens": 5}
        lines = [
            _content_chunk("hi", usage=usage),
            b"data: [DONE]\n",
        ]
        events, _, meter = _stream(
            mock.Mock(return_value=_UpstreamStream(lines)),
            env={"OPENCODE_GO_PROXY_ESTIMATE_ZERO_INPUT": "0"},
        )
        completed = next(e for e in events if e["type"] == "response.completed")["response"]
        assert completed["usage"]["input_tokens"] == 0
        assert "estimatedInputTokens" not in completed["usage"]
        assert "estimatedInputTokens" not in meter[0]

    def test_estimation_self_disables_after_real_tokens(self) -> None:
        # Turn 1 reports real non-zero input tokens; the same model's next
        # zero report is left alone (latch is one-way per model).
        real = [
            _content_chunk("a"),
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":7,"completion_tokens":1,"total_tokens":8}}\n',
            b"data: [DONE]\n",
        ]
        zero = [
            _content_chunk("b", usage={"prompt_tokens": 0, "completion_tokens": 1, "total_tokens": 1}),
            b"data: [DONE]\n",
        ]
        _stream(mock.Mock(return_value=_UpstreamStream(real)))
        events, _, meter = _stream(mock.Mock(return_value=_UpstreamStream(zero)))
        completed = next(e for e in events if e["type"] == "response.completed")["response"]
        assert completed["usage"]["input_tokens"] == 0
        assert "estimatedInputTokens" not in completed["usage"]
        assert "estimatedInputTokens" not in meter[0]

    def test_estimate_math_floor_and_cap(self) -> None:
        assert estimate_input_tokens("m", 100, {"prompt_tokens": 0}) == 1000  # floor
        assert estimate_input_tokens("m", 33000, {"prompt_tokens": 0}) == 10000
        assert estimate_input_tokens("m", 33000, {"prompt_tokens": 0}, context_window=5000) == 5000
        assert estimate_input_tokens("m", 10**7, {"prompt_tokens": 0}) == 272000  # default cap
        assert estimate_input_tokens("m", 100, {"prompt_tokens": 5}) is None  # real tokens
        assert estimate_input_tokens("m", 100, None) is None  # no usage
        assert estimate_input_tokens("m", 100, {}) is None  # no reported count

    def test_estimate_latch_and_kill_switch(self) -> None:
        note_real_input_tokens("m-latched")
        assert estimate_input_tokens("m-latched", 100, {"prompt_tokens": 0}) is None
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_ESTIMATE_ZERO_INPUT": "0"}):
            assert estimate_input_tokens("m", 100, {"prompt_tokens": 0}) is None


class TestKeepaliveToTrueEnd:
    def test_keepalive_runs_through_mid_stream_stall_and_stops_on_exit(self) -> None:
        lines = [
            _content_chunk("first"),
            _content_chunk("second"),
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n',
            b"data: [DONE]\n",
        ]
        wfile = io.BytesIO()
        cfg = make_config()
        payload = {"model": "deepseek-v4-flash", "input": "hi", "stream": True}
        with mock.patch.dict(os.environ, {
            "OPENCODE_GO_API_KEY": "test-key", "OPENCODE_GO_PROXY_KEEPALIVE_SEC": "0.05",
        }), mock.patch("urllib.request.urlopen", return_value=_StallingStream(lines, stall_sec=0.25)):
            handle_streaming_request(payload, cfg, "req", wfile)

        raw = wfile.getvalue()
        # Comments keep flowing after the first upstream byte (mid-stream stall).
        assert b": keepalive" in raw
        assert raw.find(b": keepalive") > raw.find(b"data: ")
        # No comment ever lands inside a data frame: every block still parses.
        for block in raw.decode("utf-8").split("\n\n"):
            block = block.strip()
            if block.startswith("data: ") and block != "data: [DONE]":
                json.loads(block[len("data: "):])
        # The thread stops when the stream ends; at most one in-flight comment
        # may land after return, never the ~3/0.15s a live thread would write.
        frozen = len(raw)
        time.sleep(0.3)
        assert len(wfile.getvalue()) - frozen <= len(b": keepalive\n\n")
