"""Integration tests: full HTTP round-trip with mocked upstream."""

import io
import json
import os
import threading
import urllib.request
import urllib.error
from http.client import HTTPConnection
from unittest import mock

import pytest

from opencode_go_proxy.app import ProxyConfig, ResponsesProxyHandler
from http.server import ThreadingHTTPServer


def make_config(port: int) -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=port,
        chat_base_url="https://mock-upstream.test/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=10,
        max_body_bytes=20 * 1024 * 1024,
    )


def mock_chat_response(content: str = "hello", model: str = "deepseek-v4-flash") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class MockUpstreamResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self._lines = body.split(b"\n") if b"\n" in body else [body]
        self._idx = 0
        self.status = status
        self.headers = {}

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        for line in self._lines:
            yield line

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.fixture
def server():
    """Spin up the proxy on a random port with a mocked upstream."""
    import socket
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


class TestHealthAndModels:
    def test_health_endpoint(self, server):
        port, _ = server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        assert resp.status == 200
        assert body["status"] == "ok"

    def test_v1_health_endpoint(self, server):
        port, _ = server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/v1/health")
        resp = conn.getresponse()
        conn.close()
        assert resp.status == 200

    def test_models_endpoint(self, server):
        port, _ = server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/v1/models")
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        assert resp.status == 200
        assert body["object"] == "list"
        ids = [m["id"] for m in body["data"]]
        assert "deepseek-v4-flash" in ids

    def test_404_returns_generic_message(self, server):
        port, _ = server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/nonexistent")
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        assert resp.status == 404
        assert "not found" in body["error"]["message"]
        # No path reflection
        assert "/nonexistent" not in body["error"]["message"]


class TestResponsesRoundTrip:
    def test_non_streaming_response(self, server):
        port, _ = server
        mock_resp = mock_chat_response("hello world")

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}):
            with mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(json.dumps(mock_resp).encode())):
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", "/v1/responses",
                             json.dumps({"model": "deepseek-v4-flash", "input": "Say hi."}),
                             {"content-type": "application/json"})
                resp = conn.getresponse()
                body = json.loads(resp.read())
                conn.close()

        assert resp.status == 200
        assert body["status"] == "completed"
        assert body["object"] == "response"
        # Check output text contains the mock content
        output_text = body.get("output_text", "")
        assert "hello world" in output_text

    def test_v1_responses_alias_path(self, server):
        port, _ = server
        mock_resp = mock_chat_response("hi")

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}):
            with mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(json.dumps(mock_resp).encode())):
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", "/responses",
                             json.dumps({"model": "deepseek-v4-flash", "input": "hi"}),
                             {"content-type": "application/json"})
                resp = conn.getresponse()
                body = json.loads(resp.read())
                conn.close()

        assert resp.status == 200
        assert body["status"] == "completed"

    def test_responses_compact_path(self, server):
        port, _ = server
        mock_resp = mock_chat_response("compact")

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}):
            with mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(json.dumps(mock_resp).encode())):
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", "/responses/compact",
                             json.dumps({"model": "deepseek-v4-flash", "input": "hi"}),
                             {"content-type": "application/json"})
                resp = conn.getresponse()
                body = json.loads(resp.read())
                conn.close()

        assert resp.status == 200
        assert body["status"] == "completed"

    def test_missing_api_key_returns_401(self, server):
        port, _ = server
        import opencode_go_proxy.app as app_mod
        app_mod._api_key_cache = None
        # Mock subprocess.run to return a failed completed process (no keychain entry)
        failed_completed = mock.MagicMock()
        failed_completed.returncode = 1
        failed_completed.stdout = ""
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("opencode_go_proxy.app.subprocess.run", return_value=failed_completed):
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("POST", "/v1/responses",
                             json.dumps({"model": "deepseek-v4-flash", "input": "hi"}),
                             {"content-type": "application/json"})
                resp = conn.getresponse()
                body = json.loads(resp.read())
                conn.close()

        assert resp.status == 401
        assert "OPENCODE_GO_API_KEY" in body["error"]["message"]

    def test_negative_content_length_rejected(self, server):
        port, _ = server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/v1/responses", "{}",
                     {"content-type": "application/json", "content-length": "-5"})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        assert resp.status == 400

    def test_404_on_unknown_post_path(self, server):
        port, _ = server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/unknown", "{}", {"content-type": "application/json"})
        resp = conn.getresponse()
        conn.close()
        assert resp.status == 404


class TestStreamingResponse:
    def test_streaming_sse_response(self, server):
        port, _ = server

        # Build a mock SSE stream from upstream
        sse_lines = [
            b'data: {"id":"1","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"role":"assistant","content":"hel"}}]}\n',
            b'data: {"id":"1","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"lo"}}]}\n',
            b'data: {"id":"1","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n',
            b'data: [DONE]\n',
        ]
        mock_body = b"".join(sse_lines)

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}):
            with mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(mock_body)):
                conn = HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("POST", "/v1/responses",
                             json.dumps({"model": "deepseek-v4-flash", "input": "Say hi.", "stream": True}),
                             {"content-type": "application/json"})
                resp = conn.getresponse()
                raw = resp.read()
                conn.close()

        assert resp.status == 200
        assert "text/event-stream" in resp.getheader("content-type", "")
        # Should contain response.created and response.completed events
        raw_text = raw.decode("utf-8")
        assert "response.created" in raw_text
        assert "response.completed" in raw_text
        # Should contain the text deltas
        assert "hel" in raw_text
        assert "lo" in raw_text
