"""Plan 005 protocol surface: /chat/completions passthrough, /messages 400, WS 426."""

import io
import json
import os
import socket
import threading
import time
import urllib.error
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import ClassVar
from unittest import mock

import pytest

from opencode_go_proxy.app import ProxyConfig, ResponsesProxyHandler


def make_config(port: int) -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=port,
        chat_base_url="https://mock-upstream.test/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=10,
        max_body_bytes=20 * 1024 * 1024,
    )


def http_error(code: int, body: bytes = b'{"error":{"message":"upstream down"}}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://mock-upstream.test/v1/chat/completions", code, "err", {}, io.BytesIO(body)
    )


class MockUpstreamResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status
        self.headers = {}
        # keepends=True: line iteration on a real upstream response keeps the
        # trailing newline, so a byte-for-byte relay must see it too.
        self._lines = body.splitlines(keepends=True)

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = make_config(port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), ResponsesProxyHandler)
    httpd.config = config  # type: ignore[attr-defined]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

    yield port, httpd

    httpd.shutdown()
    httpd.server_close()


def chat_body(stream: bool = False) -> bytes:
    return json.dumps({
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
    }).encode("utf-8")


def post(port: int, path: str, body: bytes) -> tuple:
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", path, body, {"content-type": "application/json"})
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp, raw


class TestChatCompletionsPassthrough:
    def test_non_streaming_relays_upstream_body_verbatim(self, server):
        port, _ = server
        upstream_body = json.dumps({
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }).encode("utf-8")

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(upstream_body)):
            resp, raw = post(port, "/v1/chat/completions", chat_body())

        assert resp.status == 200
        assert raw == upstream_body
        assert "application/json" in resp.getheader("content-type", "")

    def test_alias_path_without_v1_prefix(self, server):
        port, _ = server
        upstream_body = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode("utf-8")

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(upstream_body)):
            resp, raw = post(port, "/chat/completions", chat_body())

        assert resp.status == 200
        assert raw == upstream_body

    def test_upstream_429_status_and_body_relayed_verbatim(self, server):
        port, _ = server
        err_body = b'{"error":{"message":"over quota","type":"insufficient_quota"}}'

        with mock.patch.dict(os.environ, {
            "OPENCODE_GO_API_KEY": "test-key", "OPENCODE_GO_PROXY_MAX_RETRIES": "0",
        }), mock.patch("urllib.request.urlopen", side_effect=http_error(429, err_body)):
            resp, raw = post(port, "/v1/chat/completions", chat_body())

        assert resp.status == 429
        assert raw == err_body
        assert b"proxy_error" not in raw

    def test_upstream_500_status_and_body_relayed_verbatim(self, server):
        port, _ = server
        err_body = b'{"error":{"message":"internal boom"}}'

        with mock.patch.dict(os.environ, {
            "OPENCODE_GO_API_KEY": "test-key", "OPENCODE_GO_PROXY_MAX_RETRIES": "0",
        }), mock.patch("urllib.request.urlopen", side_effect=http_error(500, err_body)):
            resp, raw = post(port, "/v1/chat/completions", chat_body())

        assert resp.status == 500
        assert raw == err_body

    def test_streaming_relays_sse_verbatim(self, server):
        port, _ = server
        sse = (
            b'data: {"id":"1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"hel"}}]}\n\n'
            b'data: {"id":"1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n'
            b'data: {"id":"1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n'
        )

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(sse)):
            resp, raw = post(port, "/v1/chat/completions", chat_body(stream=True))

        assert resp.status == 200
        assert "text/event-stream" in resp.getheader("content-type", "")
        assert raw == sse

    def test_streaming_keepalive_comment_until_first_byte(self, server):
        port, _ = server
        first = b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"ok"}}]}\n\n'

        class SlowStream(MockUpstreamResponse):
            def __iter__(self):
                time.sleep(0.3)
                return iter(self._lines)

        with mock.patch.dict(os.environ, {
            "OPENCODE_GO_API_KEY": "test-key", "OPENCODE_GO_PROXY_KEEPALIVE_SEC": "0.05",
        }), mock.patch("urllib.request.urlopen", return_value=SlowStream(first)):
            resp, raw = post(port, "/v1/chat/completions", chat_body(stream=True))

        assert resp.status == 200
        assert b": keepalive" in raw
        assert raw.index(b": keepalive") < raw.index(b"data: ")

    def test_streaming_upstream_error_relayed_before_commit(self, server):
        port, _ = server
        err_body = b'{"error":{"message":"rate limited","type":"rate_limit_error"}}'

        with mock.patch.dict(os.environ, {
            "OPENCODE_GO_API_KEY": "test-key", "OPENCODE_GO_PROXY_MAX_RETRIES": "0",
        }), mock.patch("urllib.request.urlopen", side_effect=http_error(429, err_body)):
            resp, raw = post(port, "/v1/chat/completions", chat_body(stream=True))

        assert resp.status == 429
        assert raw == err_body
        assert "text/event-stream" not in resp.getheader("content-type", "")

    def test_missing_key_returns_401_json_non_streaming(self, server):
        port, _ = server
        from opencode_go_proxy.secrets import clear_api_key_cache

        clear_api_key_cache()
        failed = mock.MagicMock()
        failed.returncode = 1
        failed.stdout = ""
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("opencode_go_proxy.secrets.subprocess.run", return_value=failed):
            resp, raw = post(port, "/v1/chat/completions", chat_body())

        assert resp.status == 401
        assert "OPENCODE_GO_API_KEY" in json.loads(raw)["error"]["message"]

    def test_missing_key_streaming_returns_401_json_not_sse(self, server):
        port, _ = server
        from opencode_go_proxy.secrets import clear_api_key_cache

        clear_api_key_cache()
        failed = mock.MagicMock()
        failed.returncode = 1
        failed.stdout = ""
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("opencode_go_proxy.secrets.subprocess.run", return_value=failed):
            resp, raw = post(port, "/v1/chat/completions", chat_body(stream=True))

        assert resp.status == 401
        assert "text/event-stream" not in resp.getheader("content-type", "")
        assert "OPENCODE_GO_API_KEY" in json.loads(raw)["error"]["message"]

    def test_oversized_body_rejected_413(self, server):
        port, _ = server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/v1/chat/completions", b"{}", {
            "content-type": "application/json",
            "content-length": str(21 * 1024 * 1024),
        })
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()

        assert resp.status == 413
        assert b"cap" in raw


class TestMessagesEndpoint:
    EXPECTED: ClassVar[dict] = {
        "error": {
            "type": "invalid_request_error",
            "message": (
                "This proxy serves a single OpenAI-compatible provider via "
                "/v1/chat/completions and /v1/responses; /messages is not supported."
            ),
        }
    }

    def test_post_v1_messages_returns_400(self, server):
        port, _ = server
        resp, raw = post(port, "/v1/messages", b'{"model":"claude-sonnet-4"}')

        assert resp.status == 400
        assert json.loads(raw) == self.EXPECTED

    def test_post_messages_alias_returns_400(self, server):
        port, _ = server
        resp, raw = post(port, "/messages", b"{}")

        assert resp.status == 400
        assert json.loads(raw) == self.EXPECTED

    def test_get_messages_returns_400(self, server):
        port, _ = server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/v1/messages")
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()

        assert resp.status == 400
        assert json.loads(raw) == self.EXPECTED


class TestWebSocketUpgradeRejection:
    def test_upgrade_returns_exact_426_body(self, server):
        """The realtime upgrade is answered HTTP/1.1 426 with the reference body."""
        port, _ = server
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(
            b"GET /v1/responses HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Connection: Upgrade\r\n"
            b"Upgrade: websocket\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"Sec-WebSocket-Version: 13\r\n"
            b"\r\n"
        )
        sock.settimeout(5)
        chunks = []
        while True:
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        sock.close()

        data = b"".join(chunks)
        assert data == (
            b"HTTP/1.1 426 Upgrade Required\r\n"
            b"Connection: close\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
