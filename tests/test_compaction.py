"""Remote-compaction support: v1 /responses/compact and v2 trigger-item turns.

Every test drives the real proxy HTTP surface (in-process server on a scratch
port) against an http.server mock upstream that records the requests it sees,
then verifies the client-visible response, the usage-meter row, and the mock's
request log. Offline and deterministic: all traffic is loopback, the state dir
is a per-test tmp dir (conftest), and the keys are never printed.
"""

import json
import os
import socket
import threading
import time
from collections.abc import Generator
from http.client import HTTPConnection, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

from opencode_go_proxy import compaction
from opencode_go_proxy.app import ProxyConfig, ResponsesProxyHandler
from opencode_go_proxy.meter import usage_events_path
from opencode_go_proxy.secrets import clear_api_key_cache
from opencode_go_proxy.usage_poller import clear_cache as clear_usage_poller_cache

SCRATCH_PORT_PREFERRED = 8795
PROXY_TIMEOUT_SEC = 2.0
MODEL = "deepseek-v4-flash"
ZEN_MODEL = "zen/deepseek-v4-flash"
SUMMARY_TEXT = "The user asked to wire the widget to the backend; decision: REST over gRPC; open question: retry policy."
ZEN_SUMMARY_TEXT = "Zen handoff summary: migrate the catalog render, keep the compact seed."

MODE_OK = "ok"
MODE_500 = "500"

GO_PATH = "/chat/completions"
ZEN_PATH_PREFIX = "/zen/"


class RequestRecord:
    """What the mock saw for one upstream request (auth value kept in memory only)."""

    def __init__(self, path: str, headers: dict[str, str], raw_body: bytes) -> None:
        self.path = path
        self.headers = {name.lower(): value for name, value in headers.items()}
        self.content_type = self.headers.get("content-type", "")
        self.has_authorization = "authorization" in self.headers
        self.raw_body = raw_body

    @property
    def json_body(self) -> dict:
        return json.loads(self.raw_body)


class ScriptedUpstream:
    """One scripted persona: records every request, then answers per `mode`."""

    def __init__(self, mode: str = MODE_OK) -> None:
        self.mode = mode
        self.requests: list[RequestRecord] = []

    def record(self, path: str, headers: dict[str, str], raw_body: bytes) -> None:
        self.requests.append(RequestRecord(path, headers, raw_body))

    @property
    def count(self) -> int:
        return len(self.requests)

    def reset_log(self) -> None:
        self.requests.clear()

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        if self.mode == MODE_500:
            self._send_json(
                handler,
                500,
                {"error": {"type": "server_error", "message": "boom"}},
            )
            return
        self._send_json(
            handler,
            200,
            {
                "id": "chatcmpl-compact",
                "object": "chat.completion",
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": SUMMARY_TEXT},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )

    def _send_json(self, handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        handler.send_response(status)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)


class MockUpstreamHandler(BaseHTTPRequestHandler):
    """HTTP surface for the scripted upstream; every request is recorded.

    Serves both surfaces: the opencode-go chat-completions endpoint and the
    zen surface under ``/zen/`` (same chat-completion wire shape for the
    openai_chat family the tests use).
    """

    protocol_version = "HTTP/1.0"
    server_version = "MockUpstream/1.0"

    def log_message(self, message_format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or "0")
        raw_body = self.rfile.read(length) if length else b""
        self.server.behavior.record(self.path, dict(self.headers), raw_body)
        try:
            self.server.behavior.handle(self)
        except (BrokenPipeError, ConnectionResetError):
            pass


class _MockServer(ThreadingHTTPServer):
    daemon_threads = True
    behavior: ScriptedUpstream

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.behavior = ScriptedUpstream(MODE_OK)


class _ScratchProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    config: ProxyConfig


def _wait_for_listener(port: int, timeout_sec: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.01)
    raise AssertionError(f"port {port} never accepted connections")


def _pick_scratch_port() -> int:
    candidates = [SCRATCH_PORT_PREFERRED, *range(SCRATCH_PORT_PREFERRED + 1, SCRATCH_PORT_PREFERRED + 8)]
    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def scratch_port() -> Generator[int, None, None]:
    yield _pick_scratch_port()


@pytest.fixture
def mock_upstream() -> Generator[_MockServer, None, None]:
    server = _MockServer(("127.0.0.1", 0), MockUpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_for_listener(server.server_address[1])
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def proxy_env(mock_upstream: _MockServer) -> Generator[str, None, None]:
    """Scratch-proxy env: explicit fake key, fast retries, zen at the mock.

    OPENCODE_GO_API_KEY is set to a literal fake key so key resolution is
    deterministic and never consults the ambient env or the macOS keychain
    (CI has neither). OPENCODE_API_KEY is pinned empty so an inherited
    standard key cannot leak in either.
    """
    base_url = f"http://127.0.0.1:{mock_upstream.server_address[1]}"
    with mock.patch.dict(
        os.environ,
        {
            "OPENCODE_GO_API_KEY": "test-key",
            "OPENCODE_API_KEY": "",
            "OPENCODE_GO_PROXY_RETRY_BASE_MS": "1",
            "OPENCODE_GO_PROXY_MAX_RETRIES": "2",
            "OPENCODE_ZEN_BASE_URL": f"{base_url}/zen/v1",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "all_proxy": "",
            "SSL_CERT_FILE": "",
            "SSL_CERT_DIR": "",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
    ):
        clear_api_key_cache()
        clear_usage_poller_cache()
        yield base_url


@pytest.fixture
def proxy(proxy_env: str, scratch_port: int) -> Generator[_ScratchProxyServer, None, None]:
    server = _ScratchProxyServer(("127.0.0.1", scratch_port), ResponsesProxyHandler)
    server.config = ProxyConfig(
        bind="127.0.0.1",
        port=scratch_port,
        chat_base_url=proxy_env,
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=PROXY_TIMEOUT_SEC,
        max_body_bytes=20 * 1024 * 1024,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_for_listener(scratch_port)
    yield server
    server.shutdown()
    server.server_close()


def http_request(
    port: int,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout_sec: float = 15.0,
) -> tuple[int, dict[str, str], bytes]:
    conn = HTTPConnection("127.0.0.1", port, timeout=timeout_sec)
    try:
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        raw = response.read()
        status = response.status
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        return status, response_headers, raw
    finally:
        conn.close()


def post_json(port: int, path: str, payload: dict) -> tuple[int, dict[str, str], bytes]:
    raw = json.dumps(payload).encode("utf-8")
    return http_request(port, "POST", path, body=raw, headers={"content-type": "application/json"})


def open_stream(
    port: int, path: str, payload: dict, *, timeout_sec: float = 15.0
) -> tuple[HTTPConnection, HTTPResponse]:
    conn = HTTPConnection("127.0.0.1", port, timeout=timeout_sec)
    conn.request("POST", path, json.dumps(payload).encode("utf-8"), {"content-type": "application/json"})
    response = conn.getresponse()
    return conn, response


def read_until(response: HTTPResponse, marker: bytes, *, timeout_sec: float = 15.0) -> bytes:
    accumulated = bytearray()
    deadline = time.monotonic() + timeout_sec
    while marker not in accumulated:
        if time.monotonic() > deadline:
            raise AssertionError(f"stream did not reach {marker!r} within {timeout_sec}s")
        chunk = response.read1(8192)
        if not chunk:
            break
        accumulated.extend(chunk)
    return bytes(accumulated)


def meter_events() -> list[dict]:
    try:
        with open(usage_events_path(), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except OSError:
        return []


def sse_events(raw: bytes) -> list[dict]:
    """Parse the proxy's ``event:`` + ``data:`` SSE frames into event dicts."""
    events: list[dict] = []
    current: dict | None = None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line.startswith("data: "):
            payload = line[len("data: "):]
            if payload == "[DONE]":
                continue
            event = json.loads(payload)
            current = event
        elif line == "" and current is not None:
            events.append(current)
            current = None
    if current is not None:
        events.append(current)
    return events


def message_item(role: str, text: str) -> dict:
    return {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}


def conversation_input(*texts: str) -> list[dict]:
    return [message_item("user", text) for text in texts]


# ---------------------------------------------------------------------------
# Unit-level contracts (no server)
# ---------------------------------------------------------------------------


def test_encode_decode_summary_round_trip() -> None:
    encoded = compaction.encode_summary(SUMMARY_TEXT)
    assert encoded.startswith("kcr1:")
    assert compaction.decode_summary(encoded) == SUMMARY_TEXT
    # Decoder refuses foreign or malformed payloads instead of crashing.
    assert compaction.decode_summary("not-ours") is None
    assert compaction.decode_summary("kcr1:%%%") is None
    assert compaction.decode_summary(None) is None


def test_has_compaction_trigger_last_item_only() -> None:
    trigger = {"type": "context_compaction", "id": "cmp_x"}
    assert compaction.has_compaction_trigger({"input": [message_item("user", "hi"), trigger]})
    assert compaction.has_compaction_trigger({"input": [{"type": "compaction_trigger"}]})
    # A history that merely CONTAINS a compaction item is a normal turn.
    assert not compaction.has_compaction_trigger(
        {"input": [message_item("user", "hi"), trigger, message_item("user", "latest")]}
    )
    assert not compaction.has_compaction_trigger({"input": "plain string"})
    assert not compaction.has_compaction_trigger({"input": []})
    assert not compaction.has_compaction_trigger({"input": None})


def test_render_transcript_skips_trigger_and_bounds_to_tail() -> None:
    input_items = conversation_input("A" * 10, "B" * 10) + [{"type": "compaction_trigger"}]
    transcript = compaction.render_transcript(input_items, budget=12)
    # Tail-most 12 chars: the join plus the last message ("B"*10).
    assert transcript.endswith("B" * 10)
    assert "A" not in transcript
    assert compaction.render_transcript(
        conversation_input("x", "y"), budget=100
    ) == "x\n\ny"


# ---------------------------------------------------------------------------
# v1: dedicated /responses/compact endpoint
# ---------------------------------------------------------------------------


def test_v1_compact_returns_compaction_item_with_decodable_summary(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    payload = {"model": MODEL, "input": conversation_input("hello", "build the widget")}
    status, headers, raw = post_json(scratch_port, "/responses/compact", payload)
    assert status == 200, raw
    assert headers.get("content-type", "").startswith("application/json")
    body = json.loads(raw)
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["model"] == MODEL
    assert len(body["output"]) == 1
    item = body["output"][0]
    assert item["type"] == "compaction"
    assert item["id"].startswith("cmp_")
    assert compaction.decode_summary(item["encrypted_content"]) == SUMMARY_TEXT

    # One summarization sub-call against the opencode-go chat surface, non-stream.
    assert mock_upstream.behavior.count == 1
    request = mock_upstream.behavior.requests[0]
    assert request.path == GO_PATH
    chat = request.json_body
    assert chat.get("stream") is False
    assert chat["model"] == MODEL  # bare slug, not the opencode-go prefix
    assert chat["messages"][1]["content"] == compaction.COMPACT_PROMPT
    assert chat["messages"][0]["content"] == "hello\n\nbuild the widget"

    events = meter_events()
    assert len(events) == 1
    assert events[0]["status"] == 200
    assert events[0]["model"] == MODEL
    assert events[0]["provider"] == "opencode-go"
    assert events[0]["inputTokens"] == 11
    assert events[0]["outputTokens"] == 7
    assert events[0]["totalTokens"] == 18


def test_v1_compact_via_v1_prefix_path(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    payload = {"model": MODEL, "input": "prefix works"}
    status, _headers, raw = post_json(scratch_port, "/v1/responses/compact", payload)
    assert status == 200, raw
    body = json.loads(raw)
    assert body["output"][0]["type"] == "compaction"
    assert compaction.decode_summary(body["output"][0]["encrypted_content"]) == SUMMARY_TEXT
    assert mock_upstream.behavior.count == 1


# ---------------------------------------------------------------------------
# v2: trigger-item detection on the responses path
# ---------------------------------------------------------------------------


def test_v2_context_compaction_trigger_streams_expected_sse_sequence(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    payload = {
        "model": MODEL,
        "stream": True,
        "input": conversation_input("earlier", "now") + [{"type": "context_compaction", "id": "cmp_42"}],
    }
    conn, response = open_stream(scratch_port, "/v1/responses", payload)
    try:
        assert response.status == 200
        assert response.getheader("content-type", "") == "text/event-stream"
        raw = read_until(response, b"[DONE]")
    finally:
        conn.close()
    events = sse_events(raw)
    assert [event["type"] for event in events] == [
        "response.created",
        "response.output_item.added",
        "response.output_item.done",
        "response.completed",
    ]
    assert [event.get("sequence_number") for event in events] == [0, 1, 2, 3]
    added = events[1]
    assert added["output_index"] == 0
    item = added["item"]
    assert item["type"] == "context_compaction"
    assert item["id"].startswith("cmp_")
    assert compaction.decode_summary(item["encrypted_content"]) == SUMMARY_TEXT
    assert events[3]["response"]["status"] == "completed"
    assert events[3]["response"]["output"] == [item]

    # The trigger item never reaches the summarization call, and the turn
    # metered once as a 200 opencode-go turn.
    chat = mock_upstream.behavior.requests[0].json_body
    transcript = chat["messages"][0]["content"]
    assert "compaction_trigger" not in transcript
    assert "context_compaction" not in transcript
    assert "earlier\n\nnow" == transcript
    events_log = meter_events()
    assert len(events_log) == 1
    assert events_log[0]["status"] == 200
    assert events_log[0]["provider"] == "opencode-go"


def test_v2_compaction_trigger_type_also_detected(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    payload = {
        "model": MODEL,
        "stream": True,
        "input": conversation_input("history") + [{"type": "compaction_trigger"}],
    }
    conn, response = open_stream(scratch_port, "/v1/responses", payload)
    try:
        raw = read_until(response, b"[DONE]")
    finally:
        conn.close()
    events = sse_events(raw)
    assert [event["type"] for event in events][:2] == ["response.created", "response.output_item.added"]
    assert events[1]["item"]["type"] == "context_compaction"
    assert mock_upstream.behavior.count == 1


def test_v2_non_stream_returns_json_shape(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    payload = {
        "model": MODEL,
        "stream": False,
        "input": conversation_input("quiet") + [{"type": "context_compaction", "id": "cmp_7"}],
    }
    status, _headers, raw = post_json(scratch_port, "/v1/responses", payload)
    assert status == 200, raw
    body = json.loads(raw)
    assert body["status"] == "completed"
    assert body["output"][0]["type"] == "context_compaction"
    assert compaction.decode_summary(body["output"][0]["encrypted_content"]) == SUMMARY_TEXT


# ---------------------------------------------------------------------------
# Pass-through regressions: the compaction branch must not hijack normal turns
# ---------------------------------------------------------------------------


def test_normal_responses_passes_through_unchanged(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    payload = {"model": MODEL, "input": conversation_input("say something")}
    status, _headers, raw = post_json(scratch_port, "/v1/responses", payload)
    assert status == 200, raw
    body = json.loads(raw)
    assert body["status"] == "completed"
    output = body["output"]
    assert len(output) == 1
    assert output[0]["type"] == "message"
    assert output[0]["content"][0]["text"] == SUMMARY_TEXT
    # The normal path still produced one ordinary chat-completions call.
    assert mock_upstream.behavior.count == 1
    assert mock_upstream.behavior.requests[0].path == GO_PATH


def test_post_compaction_history_is_not_hijacked(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    # After a v2 compaction the app replays the compaction item inside the
    # history; the current user message stays last, so this must NOT be
    # treated as another compaction request.
    payload = {
        "model": MODEL,
        "input": [
            message_item("user", "before"),
            {"type": "context_compaction", "id": "cmp_old", "encrypted_content": compaction.encode_summary("old")},
            message_item("user", "latest question"),
        ],
    }
    status, _headers, raw = post_json(scratch_port, "/v1/responses", payload)
    assert status == 200, raw
    body = json.loads(raw)
    assert body["status"] == "completed"
    assert all(item["type"] != "context_compaction" and item["type"] != "compaction" for item in body["output"])
    assert body["output"][0]["content"][0]["text"] == SUMMARY_TEXT
    assert mock_upstream.behavior.count == 1


def test_malformed_payload_falls_through_to_normal_path(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    # A malformed trigger-shaped payload must not crash the dispatcher; it
    # takes the normal path.
    payload = {"model": MODEL, "input": "plain string"}
    status, _headers, raw = post_json(scratch_port, "/v1/responses", payload)
    assert status == 200, raw
    body = json.loads(raw)
    assert body["output"][0]["content"][0]["text"] == SUMMARY_TEXT
    assert mock_upstream.behavior.count == 1


# ---------------------------------------------------------------------------
# Summarization input contract + failure metering
# ---------------------------------------------------------------------------


def test_summarization_prompt_keeps_transcript_tail_and_drops_trigger(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    head = "A" * 40_000
    middle = "B" * 40_000
    tail = "C" * 40_000
    payload = {
        "model": MODEL,
        "input": conversation_input(head, middle, tail) + [{"type": "compaction_trigger"}],
    }
    status, _headers, raw = post_json(scratch_port, "/v1/responses", payload)
    assert status == 200, raw
    chat = mock_upstream.behavior.requests[0].json_body
    messages = chat["messages"]
    assert len(messages) == 2
    transcript = messages[0]["content"]
    assert messages[1]["content"] == compaction.COMPACT_PROMPT
    # Budget keeps the most recent 80k chars: the whole tail message, none of
    # the head, and no trigger-item residue.
    assert len(transcript) <= 80_000
    assert transcript.endswith("C" * 40_000)
    assert "A" not in transcript
    assert "compaction_trigger" not in transcript


def test_upstream_failure_surfaces_error_and_meters(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    mock_upstream.behavior.mode = MODE_500
    payload = {"model": MODEL, "input": conversation_input("try")}
    status, _headers, raw = post_json(scratch_port, "/responses/compact", payload)
    assert status == 502, raw
    body = json.loads(raw)
    assert body["error"]["type"] == "proxy_error"
    assert "upstream HTTP 500" in body["error"]["message"]
    # Initial attempt + 2 retries.
    assert mock_upstream.behavior.count == 3
    events = meter_events()
    assert len(events) == 1
    assert events[0]["status"] == 502
    assert events[0]["retries"] == 2
    assert events[0]["model"] == MODEL


# ---------------------------------------------------------------------------
# Zen routing: provider="zen" meter row, zen upstream surface
# ---------------------------------------------------------------------------


def test_zen_compaction_routes_through_zen_upstream(
    proxy: _ScratchProxyServer, mock_upstream: _MockServer, scratch_port: int
) -> None:
    mock_upstream.behavior = ScriptedUpstream(MODE_OK)
    payload = {"model": ZEN_MODEL, "input": conversation_input("zen session")}
    status, _headers, raw = post_json(scratch_port, "/responses/compact", payload)
    assert status == 200, raw
    body = json.loads(raw)
    assert body["output"][0]["type"] == "compaction"
    assert compaction.decode_summary(body["output"][0]["encrypted_content"]) == SUMMARY_TEXT
    # The zen surface (openai_chat family) received exactly one call.
    assert mock_upstream.behavior.count == 1
    request = mock_upstream.behavior.requests[0]
    assert request.path == f"{ZEN_PATH_PREFIX}v1/chat/completions"
    chat = request.json_body
    assert chat["stream"] is False
    assert chat["model"] == "deepseek-v4-flash"  # bare zen id on the wire
    assert chat["messages"][1]["content"] == compaction.COMPACT_PROMPT
    events = meter_events()
    assert len(events) == 1
    assert events[0]["status"] == 200
    assert events[0]["provider"] == "zen"
    assert events[0]["model"] == ZEN_MODEL
    assert events[0]["inputTokens"] == 11
    assert events[0]["outputTokens"] == 7
    assert events[0]["totalTokens"] == 18
