"""Integration tests: full HTTP round-trip with mocked upstream."""

import base64
import io
import json
import os
import threading
import urllib.error
import urllib.request
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
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
        self.status = status
        self.headers = {}

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        yield from self._lines

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
        # The endpoint contract is "a list of model ids"; which models are
        # present depends on the catalog the test environment provides, so the
        # assertion is on shape, not on a machine-specific model set.
        assert all(isinstance(model_id, str) and model_id for model_id in ids)

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


class TestWebSocketUpgradeRejected:
    def test_upgrade_returns_426_http11(self, server):
        """A realtime WebSocket upgrade must be answered HTTP/1.1 426 so the
        client falls back to plain HTTP streaming instead of failing on an
        HTTP/1.0 status line."""
        import socket
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
        data = sock.recv(4096)
        sock.close()
        assert data.startswith(b"HTTP/1.1 426 ")
        assert b"Upgrade Required" in data


class TestResponsesRoundTrip:
    def test_non_streaming_response(self, server):
        port, _ = server
        mock_resp = mock_chat_response("hello world")

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(json.dumps(mock_resp).encode())):
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

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(json.dumps(mock_resp).encode())):
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

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(json.dumps(mock_resp).encode())):
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
        from opencode_go_proxy.secrets import clear_api_key_cache
        clear_api_key_cache()
        # Mock subprocess.run to return a failed completed process (no keychain entry)
        failed_completed = mock.MagicMock()
        failed_completed.returncode = 1
        failed_completed.stdout = ""
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("opencode_go_proxy.secrets.subprocess.run", return_value=failed_completed):
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
        resp.read()
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

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(mock_body)):
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

    def test_streaming_missing_api_key_sends_error_event(self, server):
        port, _ = server
        from opencode_go_proxy.secrets import clear_api_key_cache
        clear_api_key_cache()
        failed_completed = mock.MagicMock()
        failed_completed.returncode = 1
        failed_completed.stdout = ""
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("opencode_go_proxy.secrets.subprocess.run", return_value=failed_completed):
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/responses",
                         json.dumps({"model": "deepseek-v4-flash", "input": "hi", "stream": True}),
                         {"content-type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()

        assert resp.status == 200
        raw_text = raw.decode("utf-8")
        assert "response.error" in raw_text
        assert "[DONE]" in raw_text

    def test_streaming_crash_sends_sse_error(self, server):
        port, _ = server
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.streaming.responses_payload_to_chat_payload",
            side_effect=ValueError("boom"),
        ):
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/responses",
                         json.dumps({"model": "deepseek-v4-flash", "input": "hi", "stream": True}),
                         {"content-type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()

        assert resp.status == 200
        raw_text = raw.decode("utf-8")
        assert "response.error" in raw_text
        assert "[DONE]" in raw_text


class TestEdgeCases:
    def test_missing_model_defaults_to_flash(self, server):
        port, _ = server
        mock_resp = mock_chat_response("ok")

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(json.dumps(mock_resp).encode())):
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/responses",
                         json.dumps({"input": "hi"}),
                         {"content-type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()

        assert resp.status == 200
        assert body["status"] == "completed"

    def test_upstream_500_returns_502(self, server):
        port, _ = server
        err = urllib.error.HTTPError(
            "https://mock.test/v1/chat/completions", 500, "Internal Server Error",
            {}, io.BytesIO(b'{"error":"server error"}'),
        )

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", side_effect=err):
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/responses",
                         json.dumps({"model": "deepseek-v4-flash", "input": "hi"}),
                         {"content-type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()

        assert resp.status == 502
        assert "proxy_error" in body["error"]["type"]

    def test_upstream_network_error_returns_502(self, server):
        port, _ = server
        err = urllib.error.URLError("timed out")

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", side_effect=err):
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/responses",
                         json.dumps({"model": "deepseek-v4-flash", "input": "hi"}),
                         {"content-type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()

        assert resp.status == 502
        assert "network error" in body["error"]["message"].lower()

    def test_upstream_429_returns_429(self, server):
        port, _ = server
        err = urllib.error.HTTPError(
            "https://mock.test/v1/chat/completions", 429, "Too Many Requests",
            {"retry-after": "10"}, io.BytesIO(b'{"error":"rate limited"}'),
        )

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", side_effect=err):
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/responses",
                         json.dumps({"model": "deepseek-v4-flash", "input": "hi"}),
                         {"content-type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()

        assert resp.status == 429
        assert "retry after" in body["error"]["message"].lower()

    def test_empty_input_string(self, server):
        port, _ = server
        mock_resp = mock_chat_response("")

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(json.dumps(mock_resp).encode())):
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/responses",
                         json.dumps({"model": "deepseek-v4-flash", "input": ""}),
                         {"content-type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()

        assert resp.status == 200
        assert body["status"] == "completed"

    def test_upstream_invalid_json_returns_502(self, server):
        port, _ = server
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(b"not json")):
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/responses",
                         json.dumps({"model": "deepseek-v4-flash", "input": "hi"}),
                         {"content-type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()

        assert resp.status == 502
        assert "invalid JSON" in body["error"]["message"]


class TestVersionFlag:
    def test_version_flag_prints_version(self):
        import subprocess

        from opencode_go_proxy import __version__
        result = subprocess.run(
            ["uv", "run", "opencode-go-proxy", "--version"],
            capture_output=True, text=True, check=False, cwd=os.path.dirname(os.path.dirname(__file__)),
        )
        assert result.returncode == 0
        assert __version__ in result.stdout


class TestGracefulShutdown:
    def test_sigterm_stops_server(self):
        """SIGTERM must stop the proxy (regression: shutdown() from a main-thread
        signal handler deadlocked when serve_forever ran on the main thread)."""
        import signal
        import socket
        import subprocess
        import sys
        import time

        env = dict(os.environ, OPENCODE_GO_API_KEY="test-key")
        proc = subprocess.Popen(
            [sys.executable, "-m", "opencode_go_proxy", "--bind", "127.0.0.1", "--port", "8799"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", 8799), timeout=1):
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                raise AssertionError("proxy did not start listening on 8799")

            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=8)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


class TestUnknownSlugFallback:
    def test_native_slug_without_capture_falls_back_to_default_model(self, server):
        # The old gpt-* -> deepseek alias hijack is gone. Without a native
        # capture the native set is empty, so the slug routes to OpenCode Go
        # and the unknown-slug fallback picks DEFAULT_MODEL. With a native
        # capture this request would never reach translation (native
        # pass-through); test_passthrough.py covers that path.
        port, _ = server
        mock_resp = mock_chat_response("ok", model="deepseek-v4-flash")

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(json.dumps(mock_resp).encode())) as mock_urlopen:
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/responses",
                         json.dumps({"model": "gpt-5.5", "input": "hi"}),
                         {"content-type": "application/json"})
            resp = conn.getresponse()
            resp.read()
            conn.close()

        assert resp.status == 200
        sent_payload = json.loads(mock_urlopen.call_args[0][0].data)
        assert sent_payload["model"] == "deepseek-v4-flash"


class TestToolCallRoundTrip:
    def test_tool_call_passes_through_http(self, server):
        port, _ = server
        mock_resp = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "deepseek-v4-flash",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_abc123",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"SF"}'},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(json.dumps(mock_resp).encode())):
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/responses",
                         json.dumps({
                             "model": "deepseek-v4-flash",
                             "input": "What's the weather in SF?",
                             "tools": [{"type": "function", "function": {
                                 "name": "get_weather",
                                 "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                             }}],
                         }),
                         {"content-type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()

        assert resp.status == 200
        assert body["status"] == "completed"
        tool_calls = [o for o in body["output"] if o["type"] == "function_call"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "get_weather"
        assert tool_calls[0]["call_id"] == "call_abc123"


class TestStreamingToolCalls:
    def test_streaming_tool_call_sse(self, server):
        port, _ = server
        sse_lines = [
            b'data: {"id":"1","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"read_file","arguments":""}}]}}]}\n',
            b'data: {"id":"1","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\":\\"README.md\\"}"}}]}}]}\n',
            b'data: {"id":"1","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n',
            b'data: [DONE]\n',
        ]
        mock_body = b"".join(sse_lines)

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", return_value=MockUpstreamResponse(mock_body)):
            conn = HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/v1/responses",
                         json.dumps({"model": "deepseek-v4-flash", "input": "read README", "stream": True}),
                         {"content-type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()

        assert resp.status == 200
        raw_text = raw.decode("utf-8")
        assert "response.created" in raw_text
        assert "response.completed" in raw_text
        assert "function_call" in raw_text
        assert "read_file" in raw_text
        # Codex requires output_item.added for each function_call;
        # items only in response.completed are silently dropped.
        assert "response.output_item.added" in raw_text
        # Verify function_call appears inside an output_item.added event
        import re
        added_events = re.findall(r'response\.output_item\.added.*?function_call', raw_text)
        assert len(added_events) >= 1, "function_call must have output_item.added event"


class TestCacheAccounting:
    def test_cache_endpoint_starts_empty(self, server):
        port, _ = server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/cache")
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        assert resp.status == 200
        assert body["totals"]["hit_ratio"] is None

    def test_non_streaming_cache_usage_reaches_codex_and_metrics(self, server):
        port, _ = server
        cached_resp = {
            "id": "chatcmpl-cache",
            "object": "chat.completion",
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 5,
                "total_tokens": 1005,
                "prompt_cache_hit_tokens": 990,
                "prompt_cache_miss_tokens": 10,
            },
        }

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "urllib.request.urlopen", return_value=MockUpstreamResponse(json.dumps(cached_resp).encode())
        ):
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/responses",
                         json.dumps({"model": "deepseek-v4-flash", "input": "hi"}),
                         {"content-type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()

        assert resp.status == 200
        # Codex sees the cache hit in the standard Responses usage shape.
        assert body["usage"]["input_tokens_details"] == {"cached_tokens": 990}

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/cache")
        metrics = json.loads(conn.getresponse().read())
        conn.close()
        assert metrics["totals"]["cache_hit_tokens"] == 990
        assert metrics["totals"]["cache_miss_tokens"] == 10
        assert metrics["totals"]["hit_ratio"] == 0.99

    def test_streaming_sets_include_usage_and_reports_cache(self, server):
        port, _ = server
        sse_lines = [
            b'data: {"id":"1","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{"content":"hi"}}]}\n',
            b'data: {"id":"1","object":"chat.completion.chunk","model":"deepseek-v4-flash","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":200,"completion_tokens":3,"total_tokens":203,"prompt_cache_hit_tokens":198,"prompt_cache_miss_tokens":2}}\n',
            b'data: [DONE]\n',
        ]
        mock_body = b"".join(sse_lines)

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "urllib.request.urlopen", return_value=MockUpstreamResponse(mock_body)
        ) as mock_urlopen:
            conn = HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/v1/responses",
                         json.dumps({"model": "deepseek-v4-flash", "input": "hi", "stream": True}),
                         {"content-type": "application/json"})
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()

        assert resp.status == 200
        raw_text = raw.decode("utf-8")
        assert "response.completed" in raw_text
        # The completed event's usage must carry the cache hit to Codex.
        assert '"cached_tokens":198' in raw_text

        # The outbound request asked the upstream to include usage in the stream.
        sent_payload = json.loads(mock_urlopen.call_args[0][0].data)
        assert sent_payload["stream"] is True
        assert sent_payload["stream_options"] == {"include_usage": True}

        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/cache")
        metrics = json.loads(conn.getresponse().read())
        conn.close()
        assert metrics["totals"]["cache_hit_tokens"] == 198
        assert metrics["totals"]["cache_miss_tokens"] == 2


class TestZstdRequests:
    def test_zstd_compressed_request_body_is_accepted(self, server):
        """The Codex desktop app sends /v1/responses bodies zstd-compressed;
        the proxy must decompress before parsing (regression: json.loads on the
        raw frame crashed with a UnicodeDecodeError)."""
        import zstandard

        port, _ = server
        mock_resp = mock_chat_response("hello from compressed")

        payload = json.dumps({"model": "deepseek-v4-flash", "input": "Say hi."}).encode()
        compressed = zstandard.ZstdCompressor().compress(payload)

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "urllib.request.urlopen", return_value=MockUpstreamResponse(json.dumps(mock_resp).encode())
        ):
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("POST", "/v1/responses", compressed,
                         {"content-type": "application/json", "content-encoding": "zstd"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()

        assert resp.status == 200
        assert body["status"] == "completed"
        assert "hello from compressed" in body.get("output_text", "")


class TestSSRFValidation:
    def test_file_scheme_rejected(self):
        from opencode_go_proxy.protocol import _is_safe_image_url
        assert not _is_safe_image_url("file:///etc/passwd")

    def test_http_scheme_rejected(self):
        from opencode_go_proxy.protocol import _is_safe_image_url
        assert not _is_safe_image_url("http://169.254.169.254/latest/meta-data/")

    def test_https_allowed(self):
        from opencode_go_proxy.protocol import _is_safe_image_url
        assert _is_safe_image_url("https://example.com/image.png")

    def test_data_image_allowed(self):
        from opencode_go_proxy.protocol import _is_safe_image_url
        assert _is_safe_image_url("data:image/png;base64,iVBORw0KGgo=")

    def test_ftp_rejected(self):
        from opencode_go_proxy.protocol import _is_safe_image_url
        assert not _is_safe_image_url("ftp://evil.com/file")


class TestImageCaptioning:
    def test_caption_replaces_image_with_text(self, server):
        port, _ = server
        caption_resp = {
            "id": "chatcmpl-cap",
            "object": "chat.completion",
            "model": "mimo-v2.5",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "A screenshot of a code editor"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
        }
        main_resp = mock_chat_response("ok")

        # First call = caption, second = main
        responses = [
            MockUpstreamResponse(json.dumps(caption_resp).encode()),
            MockUpstreamResponse(json.dumps(main_resp).encode()),
        ]

        # The local-runtime probe also rides urlopen; pin it off so it cannot
        # consume one of the fixed mock responses.
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.vision.local_runtime_enabled", return_value=False
        ), mock.patch("urllib.request.urlopen", side_effect=responses):
            conn = HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/v1/responses",
                         json.dumps({
                             "model": "deepseek-v4-flash",
                             "input": [{"type": "message", "role": "user", "content": [
                                 {"type": "input_text", "text": "What's in this image?"},
                                 {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="},
                             ]}],
                             "tools": [{"type": "function", "function": {
                                 "name": "analyze", "parameters": {"type": "object", "properties": {}},
                             }}],
                         }),
                         {"content-type": "application/json"})
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()

        assert resp.status == 200
        assert body["status"] == "completed"


class TestImageCaptionLatency:
    """Plan 001: cached captions, no transient retries, bounded fallbacks."""

    @staticmethod
    def _caption_calls(calls: list[dict]) -> list[dict]:
        return [c for c in calls if c.get("max_tokens") == 200]

    def _caption_aware_upstream(self, caption_text: str = "A screenshot of a code editor", failures: list[BaseException] | None = None):
        calls: list[dict] = []

        def handler(request, *args, **kwargs):
            payload = json.loads(request.data)
            calls.append(payload)
            if payload.get("max_tokens") == 200:
                if failures:
                    raise failures.pop(0)
                return MockUpstreamResponse(json.dumps({
                    "id": "chatcmpl-cap",
                    "object": "chat.completion",
                    "model": payload["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": caption_text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
                }).encode())
            return MockUpstreamResponse(json.dumps(mock_chat_response("ok")).encode())

        return handler, calls

    @staticmethod
    def _image_turn_payload(image_url: str) -> dict:
        return {
            "model": "deepseek-v4-flash",
            "input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "What's on screen?"},
                {"type": "input_image", "image_url": image_url},
            ]}],
            "tools": [{"type": "function", "function": {"name": "analyze", "parameters": {"type": "object", "properties": {}}}}],
        }

    def _post_turn(self, port: int, payload: dict) -> dict:
        conn = HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/v1/responses", json.dumps(payload), {"content-type": "application/json"})
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        assert resp.status == 200
        return body

    def test_identical_image_captioned_once_across_turns(self, server):
        port, _ = server
        handler, calls = self._caption_aware_upstream()
        image = "data:image/png;base64," + base64.b64encode(b"screenshot-bytes-001").decode("ascii")
        payload = self._image_turn_payload(image)

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", side_effect=handler):
            first = self._post_turn(port, payload)
            second = self._post_turn(port, payload)

        assert first["status"] == "completed"
        assert second["status"] == "completed"
        caption_calls = self._caption_calls(calls)
        assert len(caption_calls) == 1
        assert caption_calls[0]["messages"][0]["content"][1]["image_url"]["detail"] == "low"
        # The second turn's main call still carries the screenshot caption.
        main_payloads = [c for c in calls if c.get("max_tokens") != 200]
        assert len(main_payloads) == 2
        assert "[screenshot: A screenshot of a code editor]" in main_payloads[1]["messages"][0]["content"]

    def test_caption_transient_failure_degrades_without_retry(self, server):
        port, _ = server
        err = urllib.error.HTTPError(
            "https://mock.test/v1/chat/completions", 503, "Service Unavailable",
            {}, io.BytesIO(b'{"error":"down"}'),
        )
        handler, calls = self._caption_aware_upstream(failures=[err])
        payload = self._image_turn_payload("data:image/png;base64," + base64.b64encode(b"bytes-002").decode("ascii"))

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", side_effect=handler):
            body = self._post_turn(port, payload)

        assert body["status"] == "completed"
        assert len(self._caption_calls(calls)) == 1
        main_payload = next(c for c in calls if c.get("max_tokens") != 200)
        assert "[caption failed" in main_payload["messages"][0]["content"]

    def test_caption_engine_falls_back_to_mimo_on_400(self, server):
        port, _ = server
        err = urllib.error.HTTPError(
            "https://mock.test/v1/chat/completions", 400, "Bad Request",
            {}, io.BytesIO(b'{"error":"image not supported"}'),
        )
        handler, calls = self._caption_aware_upstream(failures=[err])
        payload = self._image_turn_payload("data:image/png;base64," + base64.b64encode(b"bytes-003").decode("ascii"))

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch("urllib.request.urlopen", side_effect=handler):
            body = self._post_turn(port, payload)

        assert body["status"] == "completed"
        caption_calls = self._caption_calls(calls)
        assert len(caption_calls) == 2
        assert caption_calls[0]["model"] == "deepseek-v4-flash"
        assert caption_calls[1]["model"] == "mimo-v2.5"
        assert caption_calls[1]["messages"][0]["content"][1]["image_url"]["detail"] == "low"

    def test_caption_detail_fallback_drops_detail_on_400(self, server):
        port, _ = server
        err = urllib.error.HTTPError(
            "https://mock.test/v1/chat/completions", 400, "Bad Request",
            {}, io.BytesIO(b'{"error":"unknown detail"}'),
        )
        handler, calls = self._caption_aware_upstream(failures=[err])
        payload = self._image_turn_payload("data:image/png;base64," + base64.b64encode(b"bytes-004").decode("ascii"))
        image_url = payload["input"][0]["content"][1]["image_url"]

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key", "CODEX_IMAGE_MODEL": "mimo-v2.5"}), mock.patch("urllib.request.urlopen", side_effect=handler):
            body = self._post_turn(port, payload)

        assert body["status"] == "completed"
        caption_calls = self._caption_calls(calls)
        assert len(caption_calls) == 2
        # Engine is already mimo, so only the detail fallback fires: same URL,
        # no detail key (garbage bytes cannot be downscaled, so the URL stays).
        assert caption_calls[0]["messages"][0]["content"][1]["image_url"] == {"url": image_url, "detail": "low"}
        assert caption_calls[1]["messages"][0]["content"][1]["image_url"] == {"url": image_url}
