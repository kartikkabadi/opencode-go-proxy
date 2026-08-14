"""Unit tests for the SSE streaming engine (streaming.py)."""

import io
import json
import os
import socket
import struct
import time
from unittest import mock

from opencode_go_proxy.app import ProxyConfig
from opencode_go_proxy.meter import usage_events_path
from opencode_go_proxy.secrets import clear_api_key_cache
from opencode_go_proxy.streaming import _client_gone, handle_streaming_request


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
    meter: list[dict] = []
    with mock.patch.dict(os.environ, merged):
        handle_streaming_request(payload, cfg, "req", wfile)
        try:
            with open(usage_events_path(), encoding="utf-8") as f:
                meter = [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            meter = []
    events: list[dict] = []
    for block in wfile.getvalue().decode("utf-8").split("\n\n"):
        block = block.strip()
        if not block.startswith("data: "):
            continue
        data = block[len("data: "):]
        if data == "[DONE]":
            continue
        events.append(json.loads(data))

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
    def test_no_sse_data_is_metered_as_502_not_empty(self, tmp_path) -> None:
        # An upstream that opens but never sends data is an upstream failure
        # (502), not an empty 200; it must not carry the emptyCompletion marker.
        state = str(tmp_path / "state")
        with mock.patch("urllib.request.urlopen", return_value=_UpstreamStream([])):
            events, wfile, meter = _stream([], state_dir=state)
        assert any(e["type"] == "response.error" for e in events)
        assert b"[DONE]" in wfile.getvalue()
        assert meter, "expected a meter record"
        record = meter[0]
        assert record["status"] == 502
        assert record.get("emptyCompletion") is not True

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

    def test_empty_completion_metered_as_502(self, tmp_path) -> None:
        # Two empty 200s (the retry also empty) end as response.error
        # empty_completion; the client-visible outcome is a failed turn, so
        # the meter records 502 with the emptyCompletion marker, never 200.
        state = str(tmp_path / "state")
        lines = [b"data: [DONE]\n"]
        with mock.patch("urllib.request.urlopen", return_value=_UpstreamStream(lines)):
            events, wfile, meter = _stream(lines, state_dir=state)
        errors = [e for e in events if e["type"] == "response.error"]
        assert len(errors) == 1
        assert errors[0]["error"].get("code") == "empty_completion"
        assert b"[DONE]" in wfile.getvalue()
        assert meter
        record = meter[0]
        assert record["status"] == 502
        assert record.get("emptyCompletion") is True
        assert record.get("retries") == 1


class TestOutputIndexes:
    def test_mixed_text_and_tool_calls_get_unique_indexes(self) -> None:
        # Regression: a message and the first function call must never share
        # an output_index (they used to collide when both appeared in one turn).
        lines = [
            b'data: {"choices":[{"delta":{"content":"reading "}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"read_file","arguments":""}}]}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\":\\"/tmp/x\\"}"}}]}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n',
            b"data: [DONE]\n",
        ]
        with mock.patch("urllib.request.urlopen", return_value=_UpstreamStream(lines)):
            events, _, _meter = _stream(lines)
        added = [e for e in events if e["type"] == "response.output_item.added"]
        assert len(added) == 2, added
        indices = [e["output_index"] for e in added]
        assert len(set(indices)) == len(indices), f"collision: {indices}"
        done = [e for e in events if e["type"] == "response.output_item.done"]
        done_indices = [e["output_index"] for e in done]
        assert len(set(done_indices)) == len(done_indices), f"done collision: {done_indices}"


class TestFinalOnlyToolIndex:
    def test_name_only_tool_call_gets_unique_index(self) -> None:
        # A tool call that never streams arguments (final-only) must not reuse
        # output_index 0 or collide with any other item.
        lines = [
            b'data: {"choices":[{"delta":{"content":"reading "}}]}\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"read_file"}}]}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n',
            b"data: [DONE]\n",
        ]
        with mock.patch("urllib.request.urlopen", return_value=_UpstreamStream(lines)):
            events, _wfile, _meter = _stream(lines)
        added = [e for e in events if e["type"] == "response.output_item.added"]
        assert len(added) >= 2
        indices = [e["output_index"] for e in added]
        assert len(set(indices)) == len(indices), f"collision: {indices}"


class TestClientGone:
    """_client_gone peeks the client socket; a closed peer must read as gone.

    On macOS a write to a reset peer does not error, so this peek is the only
    way a cancelling client becomes visible before the stream ends.
    """

    @staticmethod
    def _assert_eventually_gone(sock: socket.socket) -> None:
        wfile = sock.makefile("wb", 0)  # same shape as handler.wfile
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if _client_gone(wfile):
                return
            time.sleep(0.005)
        raise AssertionError("closed peer was never detected as gone")

    def test_alive_peer_is_not_gone(self) -> None:
        server, client = socket.socketpair()
        try:
            assert _client_gone(server.makefile("wb", 0)) is False
        finally:
            server.close()
            client.close()

    def test_peer_fin_reads_as_gone(self) -> None:
        server, client = socket.socketpair()
        try:
            client.close()
            self._assert_eventually_gone(server)
        finally:
            server.close()

    def test_peer_rst_reads_as_gone(self) -> None:
        server, client = socket.socketpair()
        try:
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            client.close()
            self._assert_eventually_gone(server)
        finally:
            server.close()

    def test_socketless_wfile_reads_as_alive(self) -> None:
        # Test doubles (io.BytesIO) have no peekable socket; the probe must
        # degrade to the old write-only behavior, never crash the stream.
        assert _client_gone(io.BytesIO()) is False
