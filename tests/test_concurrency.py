"""Concurrent requests through a real local proxy must not interleave or corrupt."""
import json
import os
import socket
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import pytest

from opencode_go_proxy.app import ResponsesProxyHandler
from opencode_go_proxy.config import ProxyConfig

N = 10


class MockUpstreamHandler(BaseHTTPRequestHandler):
    lock = threading.Lock()
    requests = 0

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        with type(self).lock:
            type(self).requests += 1
        time.sleep((type(self).requests % 5) * 0.01)  # 0-40ms variance
        marker = "?"
        messages = body.get("messages") or []
        if messages and isinstance(messages[0], dict):
            content = messages[0].get("content")
            if isinstance(content, str):
                marker = content.replace("echo-", "echo:")
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        marker = part.get("text", "?").replace("echo-", "echo:")
        if body.get("stream"):
            chunks = marker
            payload = "".join(
                f'data: {json.dumps({"choices": [{"delta": {"content": c}}]})}\n\n'
                for c in chunks
            ) + "data: [DONE]\n\n"
            data = payload.encode()
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            resp = {
                "id": "cmpl-mock",
                "object": "chat.completion",
                "model": "mock",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": f"echo:{marker}"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            }
            data = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def pair():
    up_port = _free_port()
    upstream = ThreadingHTTPServer(("127.0.0.1", up_port), MockUpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    port = _free_port()
    config = ProxyConfig(
        bind="127.0.0.1",
        port=port,
        chat_base_url=f"http://127.0.0.1:{up_port}",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=15,
        max_body_bytes=20 * 1024 * 1024,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", port), ResponsesProxyHandler)
    httpd.config = config  # type: ignore[attr-defined]
    proxy_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    proxy_thread.start()
    yield port
    httpd.shutdown()
    httpd.server_close()
    upstream.shutdown()
    upstream.server_close()


def _post(port: int, body: dict, stream: bool = False) -> dict:
    payload = dict(body)
    if stream:
        payload["stream"] = True
    data = json.dumps(payload).encode()
    conn = HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request("POST", "/v1/responses", body=data, headers={"content-type": "application/json"})
    resp = conn.getresponse()
    raw = resp.read().decode()
    conn.close()
    assert resp.status == 200, raw[:200]
    if stream:
        events = [ln[6:] for ln in raw.splitlines() if ln.startswith("data: ") and ln[6:] != "[DONE]"]
        assert events, "no SSE events"
        chunks = [
            json.loads(e).get("delta", "")
            for e in events
            if json.loads(e).get("type") == "response.output_text.delta"
        ]
        return {"status": "streamed", "output_text": "".join(chunks)}
    return json.loads(raw)


def test_concurrent_nonstream_requests_do_not_interleave(pair) -> None:
    port = pair
    results: dict[int, str] = {}
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            resp = _post(port, {"model": "deepseek-v4-flash", "input": f"echo-{i}"})
            results[i] = resp["output_text"]
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    # One env patch on the main thread; per-thread patch.dict races on PATH.
    with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}, clear=True):
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    assert not errors, errors
    assert len(results) == N
    for i in range(N):
        assert f"echo:{i}" in results[i], f"request {i} got {results[i]!r}"


def test_concurrent_streams_are_isolated(pair) -> None:
    port = pair
    markers: list[str] = []
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            resp = _post(port, {"model": "deepseek-v4-flash", "input": f"echo-{i}"}, stream=True)
            markers.append(resp["output_text"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}, clear=True):
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    assert not errors, errors
    assert len(markers) == 3
    assert {m.strip() for m in markers} == {f"echo:{i}" for i in range(3)}


def test_server_healthy_after_concurrency(pair) -> None:
    port = pair
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/health")
    resp = conn.getresponse()
    assert resp.status == 200
    assert b'"status":"ok"' in resp.read()
    conn.close()
