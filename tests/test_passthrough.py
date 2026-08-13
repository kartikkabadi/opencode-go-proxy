"""Native passthrough: verbatim relay with the client's own auth."""

import email.message
import io
import json
import os
import socket
import threading
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from unittest import mock

import pytest

from opencode_go_proxy import passthrough
from opencode_go_proxy.app import ProxyConfig, ResponsesProxyHandler
from opencode_go_proxy.meter import state_dir, usage_events_path
from opencode_go_proxy.passthrough import relay_native_request


class _FakeChatGpt(BaseHTTPRequestHandler):
    """Fake chatgpt.com backend: records requests, serves json/sse/error."""
    captured: ClassVar[list[dict]] = []
    mode = "json"
    status = 200

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        type(self).captured.append(
            {
                "path": self.path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": body,
            }
        )
        if type(self).mode == "sse":
            payload = (
                b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
                b"data: [DONE]\n\n"
            )
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if type(self).status != 200:
            error_body = json.dumps({"error": {"message": "boom"}}).encode()
            self.send_response(type(self).status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
            return
        payload = json.dumps(
            {
                "id": "resp_native",
                "object": "response",
                "status": "completed",
                "model": "gpt-5.6-luna",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ],
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def native_upstream():
    _FakeChatGpt.captured = []
    _FakeChatGpt.mode = "json"
    _FakeChatGpt.status = 200
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _FakeChatGpt)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()


class _FakeHandler:
    """Minimal stand-in for ResponsesProxyHandler: headers in, wire out."""

    def __init__(self, headers: dict[str, str]) -> None:
        message = email.message.Message()
        for name, value in headers.items():
            message[name] = value
        self.headers = message
        self.status = None
        self.response_headers: dict[str, str] = {}
        self.wfile = io.BytesIO()

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.response_headers[name.lower()] = str(value)

    def end_headers(self) -> None:
        pass


def make_config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=8787,
        chat_base_url="https://opencode.ai/zen/go/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=10,
        max_body_bytes=20 * 1024 * 1024,
    )


def _meter_events() -> list[dict]:
    with open(usage_events_path()) as handle:
        return [json.loads(line) for line in handle if line.strip()]


def test_relays_non_stream_with_client_auth(native_upstream) -> None:
    _FakeChatGpt.mode = "json"
    handler = _FakeHandler(
        {
            "authorization": "Bearer client-chatgpt-token",
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "codex/1.0-test",
            "x-test-forward": "yes",
            "x-opencode-go-secret": "must-not-leak",
            "x-opencode-go-model": "whatever",
        }
    )
    payload = {"model": "gpt-5.6-luna", "input": "hi", "stream": False}
    with mock.patch.dict(os.environ, {passthrough.NATIVE_BASE_URL_ENV: native_upstream}):
        relay_native_request(handler, payload, make_config(), "req-native")

    captured = _FakeChatGpt.captured[0]
    assert captured["path"] == "/v1/responses"
    assert captured["headers"]["authorization"] == "Bearer client-chatgpt-token"
    assert captured["headers"]["x-test-forward"] == "yes"
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["headers"]["user-agent"] == "codex/1.0-test"
    assert "x-opencode-go-secret" not in captured["headers"]
    assert "x-opencode-go-model" not in captured["headers"]
    assert json.loads(captured["body"]) == payload

    assert handler.status == 200
    assert handler.response_headers["content-type"] == "application/json"
    assert json.loads(handler.wfile.getvalue())["model"] == "gpt-5.6-luna"


def test_never_attaches_opencode_go_key(native_upstream) -> None:
    handler = _FakeHandler({"authorization": "Bearer client-token", "content-type": "application/json"})
    with mock.patch.dict(
        os.environ,
        {
            passthrough.NATIVE_BASE_URL_ENV: native_upstream,
            "OPENCODE_GO_API_KEY": "sk-opencode-go-secret",
        },
    ):
        relay_native_request(
            handler, {"model": "gpt-5.6-luna", "input": "hi"}, make_config(), "req"
        )
    captured = _FakeChatGpt.captured[0]
    assert "sk-opencode-go-secret" not in json.dumps(captured["headers"])


def test_native_turn_is_metered(native_upstream) -> None:
    handler = _FakeHandler({"authorization": "Bearer t", "content-type": "application/json"})
    with mock.patch.dict(os.environ, {passthrough.NATIVE_BASE_URL_ENV: native_upstream}):
        relay_native_request(
            handler, {"model": "gpt-5.6-luna", "input": "hi"}, make_config(), "req"
        )
    events = _meter_events()
    assert len(events) == 1
    assert events[0]["model"] == "gpt-5.6-luna"
    assert events[0]["status"] == 200
    assert events[0]["provider"] == "native"


def test_stream_relays_sse_verbatim(native_upstream) -> None:
    _FakeChatGpt.mode = "sse"
    handler = _FakeHandler(
        {
            "authorization": "Bearer client-token",
            "content-type": "application/json",
            "accept": "text/event-stream",
        }
    )
    with mock.patch.dict(
        os.environ,
        {
            passthrough.NATIVE_BASE_URL_ENV: native_upstream,
            "OPENCODE_GO_PROXY_KEEPALIVE_SEC": "0.05",
        },
    ):
        relay_native_request(
            handler,
            {"model": "gpt-5.6-luna", "input": "hi", "stream": True},
            make_config(),
            "req-stream",
        )
    raw = handler.wfile.getvalue()
    assert raw.startswith(b'data: {"type":"response.output_text.delta"')
    assert b"data: [DONE]" in raw
    assert handler.status == 200
    assert handler.response_headers["content-type"] == "text/event-stream"
    events = _meter_events()
    assert events[-1]["provider"] == "native"
    assert events[-1]["model"] == "gpt-5.6-luna"


def test_upstream_error_relayed_verbatim(native_upstream) -> None:
    _FakeChatGpt.status = 429
    handler = _FakeHandler({"authorization": "Bearer t", "content-type": "application/json"})
    with mock.patch.dict(os.environ, {passthrough.NATIVE_BASE_URL_ENV: native_upstream}):
        relay_native_request(
            handler, {"model": "gpt-5.6-luna", "input": "hi"}, make_config(), "req"
        )
    assert handler.status == 429
    assert json.loads(handler.wfile.getvalue())["error"]["message"] == "boom"
    events = _meter_events()
    assert events[0]["status"] == 429
    assert events[0]["provider"] == "native"


def _seed_native_capture() -> None:
    state = state_dir()
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "native-models.json"), "w") as handle:
        json.dump(
            {"captured_at": "2026-08-13T00:00:00Z", "models": [{"slug": "gpt-5.6-luna"}]},
            handle,
        )


def _proxy_server() -> tuple[int, ThreadingHTTPServer]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), ResponsesProxyHandler)
    httpd.config = make_config()  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return port, httpd


def test_proxy_dispatches_native_model_to_passthrough(native_upstream) -> None:
    _seed_native_capture()
    port, httpd = _proxy_server()
    try:
        with mock.patch.dict(
            os.environ,
            {
                passthrough.NATIVE_BASE_URL_ENV: native_upstream,
                "OPENCODE_GO_API_KEY": "sk-should-not-be-forwarded",
            },
        ):
            conn = HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request(
                "POST",
                "/v1/responses",
                json.dumps({"model": "gpt-5.6-luna", "input": "hi"}).encode(),
                {"content-type": "application/json"},
            )
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert resp.status == 200
    assert json.loads(raw)["model"] == "gpt-5.6-luna"
    captured = _FakeChatGpt.captured[0]
    assert captured["path"] == "/v1/responses"
    assert "sk-should-not-be-forwarded" not in json.dumps(captured["headers"])


def test_proxy_dispatches_native_stream_to_passthrough(native_upstream) -> None:
    _seed_native_capture()
    _FakeChatGpt.mode = "sse"
    port, httpd = _proxy_server()
    try:
        with mock.patch.dict(os.environ, {passthrough.NATIVE_BASE_URL_ENV: native_upstream}):
            conn = HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request(
                "POST",
                "/v1/responses",
                json.dumps({"model": "gpt-5.6-luna", "input": "hi", "stream": True}).encode(),
                {"content-type": "application/json"},
            )
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert resp.status == 200
    assert b"data: [DONE]" in raw
    assert b'data: {"type":"response.output_text.delta"' in raw
    events = _meter_events()
    assert events[-1]["provider"] == "native"


def test_proxy_opencode_go_model_still_translates(native_upstream) -> None:
    """A non-native model must NOT be relayed to the native backend."""
    from opencode_go_proxy.secrets import clear_api_key_cache

    clear_api_key_cache()
    raw = json.dumps(
        {
            "id": "cmpl-x",
            "object": "chat.completion",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "hi"},
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    ).encode("utf-8")
    port, httpd = _proxy_server()
    try:
        with mock.patch.dict(
            os.environ,
            {
                passthrough.NATIVE_BASE_URL_ENV: native_upstream,
                "OPENCODE_GO_API_KEY": "test-key",
            },
        ), mock.patch(
            "urllib.request.urlopen",
            return_value=mock.Mock(
                status=200,
                headers={},
                read=lambda: raw,
                __enter__=lambda self: self,
                __exit__=lambda *args: False,
            ),
        ):
            conn = HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request(
                "POST",
                "/v1/responses",
                json.dumps({"model": "opencode-go/deepseek-v4-flash", "input": "hi"}).encode(),
                {"content-type": "application/json"},
            )
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert resp.status == 200
    assert json.loads(body)["output_text"] == "hi"
    # The native backend never saw an opencode-go request.
    assert _FakeChatGpt.captured == []
