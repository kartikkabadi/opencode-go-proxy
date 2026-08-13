"""Plan 006 auth transport guard: loopback Host, browser rejection, JSON content type."""

import json
import os
import socket
import threading
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


class MockUpstreamResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status
        self.headers = {}
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


def chat_body() -> bytes:
    return json.dumps({
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }).encode("utf-8")


def upstream_ok() -> MockUpstreamResponse:
    return MockUpstreamResponse(json.dumps({
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "deepseek-v4-flash",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }).encode("utf-8"))


def post(port: int, path: str, body: bytes, headers: dict | None = None) -> tuple:
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "POST",
        path,
        body,
        {"content-type": "application/json"} if headers is None else headers,
    )
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp, raw


def raw_request(port: int, data: bytes) -> bytes:
    """Send a raw HTTP request so tests can omit or forge headers."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(data)
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
    return b"".join(chunks)


def split_raw(raw: bytes) -> tuple[bytes, bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    return head, body


class TestHostValidation:
    def test_missing_host_returns_400_invalid_host(self, server):
        port, _ = server
        raw = raw_request(
            port,
            b"POST /v1/responses HTTP/1.1\r\n"
            b"content-type: application/json\r\n"
            b"content-length: 2\r\n"
            b"connection: close\r\n"
            b"\r\n{}",
        )
        head, body = split_raw(raw)

        assert b" 400 " in head
        assert json.loads(body) == {
            "error": {"type": "invalid_host", "message": "missing Host header"}
        }

    def test_evil_host_returns_403_invalid_host(self, server):
        port, _ = server
        resp, raw = post(port, "/v1/chat/completions", chat_body(), {
            "content-type": "application/json",
            "Host": "evil.example.com",
        })

        assert resp.status == 403
        assert json.loads(raw) == {
            "error": {"type": "invalid_host", "message": "request host is not allowed"}
        }

    def test_evil_host_with_port_returns_403(self, server):
        port, _ = server
        resp, raw = post(port, "/v1/chat/completions", chat_body(), {
            "content-type": "application/json",
            "Host": "evil.example.com:8787",
        })

        assert resp.status == 403
        assert json.loads(raw)["error"]["type"] == "invalid_host"

    def test_evil_host_cannot_read_get_responses(self, server):
        """DNS rebinding must not be able to read responses, not just POSTs."""
        port, _ = server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/v1/models", headers={"Host": "evil.example.com"})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()

        assert resp.status == 403
        assert json.loads(raw)["error"]["type"] == "invalid_host"

    def test_loopback_host_with_port_passes(self, server):
        port, _ = server
        raw = raw_request(
            port,
            b"GET /health HTTP/1.1\r\n"
            b"Host: 127.0.0.1:8787\r\n"
            b"connection: close\r\n"
            b"\r\n",
        )
        head, body = split_raw(raw)
        assert b" 200 " in head
        assert json.loads(body) == {"status": "ok"}

    def test_localhost_and_ipv6_loopback_passes(self, server):
        port, _ = server
        for host in ("localhost", "[::1]:8787"):
            head, _ = split_raw(raw_request(
                port,
                f"GET /health HTTP/1.1\r\nHost: {host}\r\nconnection: close\r\n\r\n".encode(),
            ))
            assert b" 200 " in head, host


class TestBrowserRejection:
    def test_origin_header_returns_403_browser_request_rejected(self, server):
        port, _ = server
        resp, raw = post(port, "/v1/chat/completions", chat_body(), {
            "content-type": "application/json",
            "Origin": "https://example.com",
        })

        assert resp.status == 403
        assert json.loads(raw) == {
            "error": {
                "type": "browser_request_rejected",
                "message": "Browser-originated requests are not accepted by the local proxy.",
            }
        }

    def test_referer_header_returns_403(self, server):
        port, _ = server
        resp, raw = post(port, "/v1/chat/completions", chat_body(), {
            "content-type": "application/json",
            "Referer": "https://example.com/app",
        })

        assert resp.status == 403
        assert json.loads(raw)["error"]["type"] == "browser_request_rejected"

    def test_sec_fetch_site_header_returns_403(self, server):
        port, _ = server
        resp, raw = post(port, "/v1/chat/completions", chat_body(), {
            "content-type": "application/json",
            "Sec-Fetch-Site": "cross-site",
        })

        assert resp.status == 403
        assert json.loads(raw)["error"]["type"] == "browser_request_rejected"

    def test_browser_get_cannot_read_models(self, server):
        port, _ = server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/v1/models", headers={"Sec-Fetch-Site": "same-origin"})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()

        assert resp.status == 403
        assert json.loads(raw)["error"]["type"] == "browser_request_rejected"


class TestContentTypeGuard:
    def test_text_plain_post_returns_415(self, server):
        port, _ = server
        resp, raw = post(port, "/v1/responses", b"hello", {"content-type": "text/plain"})

        assert resp.status == 415
        assert json.loads(raw) == {
            "error": {
                "type": "unsupported_media_type",
                "message": "Proxy requests require Content-Type: application/json.",
            }
        }

    def test_missing_content_type_returns_415(self, server):
        port, _ = server
        resp, raw = post(port, "/v1/chat/completions", chat_body(), {})

        assert resp.status == 415
        assert json.loads(raw)["error"]["type"] == "unsupported_media_type"

    def test_charset_param_is_accepted(self, server):
        port, _ = server
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", return_value=upstream_ok()):
            resp, raw = post(port, "/v1/chat/completions", chat_body(), {
                "content-type": "application/json; charset=utf-8",
            })

        assert resp.status == 200
        assert b"hi" in raw


class TestOptionsPreflight:
    def test_options_is_unhandled_and_has_no_cors_headers(self, server):
        port, _ = server
        raw = raw_request(
            port,
            b"OPTIONS /v1/responses HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Origin: https://example.com\r\n"
            b"Access-Control-Request-Method: POST\r\n"
            b"connection: close\r\n"
            b"\r\n",
        )
        head, _ = split_raw(raw)

        assert b" 501 " in head
        assert b"access-control-allow-origin" not in raw.lower()
        assert b"access-control-allow-methods" not in raw.lower()


class TestAllowRemoteEscapeHatch:
    def test_allow_remote_bypasses_host_check_only(self, server):
        port, _ = server
        with mock.patch.dict(os.environ, {
            "OPENCODE_GO_API_KEY": "test-key",
            "OPENCODE_GO_PROXY_ALLOW_REMOTE": "1",
        }), mock.patch("urllib.request.urlopen", return_value=upstream_ok()):
            resp, raw = post(port, "/v1/chat/completions", chat_body(), {
                "content-type": "application/json",
                "Host": "proxy.example.net",
            })

        assert resp.status == 200
        assert b"hi" in raw

    def test_allow_remote_does_not_weaken_browser_rejection(self, server):
        port, _ = server
        with mock.patch.dict(os.environ, {
            "OPENCODE_GO_API_KEY": "test-key",
            "OPENCODE_GO_PROXY_ALLOW_REMOTE": "1",
        }), mock.patch("urllib.request.urlopen", return_value=upstream_ok()):
            resp, raw = post(port, "/v1/chat/completions", chat_body(), {
                "content-type": "application/json",
                "Host": "proxy.example.net",
                "Origin": "https://example.com",
            })

        assert resp.status == 403
        assert json.loads(raw)["error"]["type"] == "browser_request_rejected"


class TestValidRequestsPass:
    def test_loopback_json_post_still_reaches_upstream(self, server):
        port, _ = server
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
             mock.patch("urllib.request.urlopen", return_value=upstream_ok()):
            resp, raw = post(port, "/v1/chat/completions", chat_body())

        assert resp.status == 200
        assert b"hi" in raw
