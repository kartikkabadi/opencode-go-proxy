"""Unit tests for the SSE streaming engine (streaming.py)."""

import io
import json
import os
from unittest import mock

from opencode_go_proxy.app import ProxyConfig
from opencode_go_proxy.meter import usage_events_path
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


def _stream(lines: list[bytes], *, env: dict[str, str] | None = None, state_dir: str | None = None):
    """Drive handle_streaming_request; returns (events, wfile, meter_events)."""
    clear_api_key_cache()
    merged = {"OPENCODE_GO_API_KEY": "test-key"}
    if state_dir:
        merged["OPENCODE_GO_PROXY_STATE_DIR"] = state_dir
    if env:
        merged.update(env)
    cfg = make_config()
    payload = {"model": "deepseek-v4-flash", "input": "hi", "stream": True}
    wfile = io.BytesIO()
    with mock.patch.dict(os.environ, merged):
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


class TestKeepalive:
    def test_keepalive_thread_starts_and_stream_completes(self) -> None:
        # The keepalive thread runs on every stream. A successful stream must
        # complete with the keepalive stopped: if the stop event were never
        # set, the daemon thread would still be parked in wait(15), and the
        # fast upstream would leave zero keepalive bytes on the wire.
        lines = [
            b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"hi"}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n',
            b'data: [DONE]\n',
        ]
        with mock.patch("urllib.request.urlopen", return_value=_UpstreamStream(lines)):
            events, wfile, _ = _stream(lines)
        assert any(e["type"] == "response.completed" for e in events)
        # No keepalive comments: the upstream answered immediately, so the
        # keepalive loop must have been stopped before its 15s tick.
        assert b": keepalive" not in wfile.getvalue()


class TestOutputIndex:
    def test_monotonic_after_reasoning(self) -> None:
        lines = [
            b'data: {"id":"1","choices":[{"index":0,"delta":{"reasoning_content":"think"}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"answer"}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n',
            b'data: [DONE]\n',
        ]
        with mock.patch("urllib.request.urlopen", return_value=_UpstreamStream(lines)):
            events, _, _ = _stream(lines)
        # Each output item keeps its own index: reasoning occupies 0 and text
        # takes 1, so text deltas can never collide with the reasoning item.
        added = [e for e in events if e["type"] == "response.output_item.added"]
        assert added[0]["output_index"] == 0
        assert added[1]["output_index"] == 1


class TestEmptyCompletion:
    def test_no_sse_data_meters_empty_completion(self, tmp_path) -> None:
        # An upstream that opens but never sends data must produce an SSE
        # error and an empty_completion meter record, not a success.
        state = str(tmp_path / "state")
        with mock.patch("urllib.request.urlopen", return_value=_UpstreamStream([])):
            events, wfile, meter = _stream([], state_dir=state)
        assert any(e["type"] == "response.error" for e in events)
        assert b"[DONE]" in wfile.getvalue()
        assert meter, "expected a meter record"
        record = meter[0]
        assert record["status"] == 502
        assert record.get("emptyCompletion") is True

    def test_success_is_not_empty(self, tmp_path) -> None:
        state = str(tmp_path / "state")
        lines = [
            b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"hi"}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n',
            b'data: [DONE]\n',
        ]
        with mock.patch("urllib.request.urlopen", return_value=_UpstreamStream(lines)):
            _, _, meter = _stream(lines, state_dir=state)
        assert meter
        record = meter[0]
        assert record["status"] == 200
        assert not record.get("emptyCompletion")
