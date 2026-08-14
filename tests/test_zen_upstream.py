"""Unit + integration tests for the zen upstream client (zen_upstream.py).

No network and no real keys: ``urllib.request.urlopen`` is mocked (or pointed
at a local fake server) and the API key comes from a monkeypatched
``resolve_api_key``. The zen family map is monkeypatched so tests never depend
on the machine's zen capture files.
"""

import http.client
import io
import json
import os
import threading
import urllib.error
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from unittest import mock

import pytest

from opencode_go_proxy import zen_upstream
from opencode_go_proxy.app import ProxyConfig
from opencode_go_proxy.errors import ProxyError
from opencode_go_proxy.meter import usage_events_path
from opencode_go_proxy.secrets import clear_api_key_cache

# Bare zen id -> family, mirroring the docs the catalog resolves from.
FIXTURE_FAMILIES = {
    "claude-sonnet-4": "anthropic_messages",
    "qwen3.5-max": "anthropic_messages",
    "gemini-3-pro": "google_gemini",
    "gpt-5.6": "openai_responses",
    "grok-4": "openai_responses",
    "deepseek-v4-flash": "openai_chat",
    "glm-5": "openai_chat",
}


def make_config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1", port=8787, chat_base_url="https://go.test/v1",
        api_key_env="OPENCODE_GO_API_KEY", timeout_sec=10, max_body_bytes=1024 * 1024,
    )


class _UpstreamStream:
    def __init__(self, lines: list[bytes], status: int = 200, headers: dict | None = None) -> None:
        self._lines = list(lines)
        self.status = status
        self.headers = headers or {}

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in: records status/headers, writes to wfile."""

    def __init__(self) -> None:
        self.wfile = io.BytesIO()
        self.status = None
        self.headers: list[tuple[str, str]] = []
        self._head_committed = False

    def send_response(self, status: int) -> None:
        self.status = status
        self._head_committed = True

    def send_header(self, name: str, value: str) -> None:
        self.headers.append((name, value))

    def end_headers(self) -> None:
        pass

    def header(self, name: str) -> str | None:
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None

    def body(self) -> bytes:
        return self.wfile.getvalue()

    def events(self) -> list[dict]:
        events = []
        for block in self.body().decode("utf-8").split("\n\n"):
            block = block.strip()
            if not block.startswith("data: "):
                continue
            data = block[len("data: "):]
            if data == "[DONE]":
                continue
            events.append(json.loads(data))
        return events


def http_error(code: int, body: bytes = b'{"type":"error","error":{"type":"AuthError","message":"boom"}}', retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {}
    if retry_after:
        headers["retry-after"] = retry_after
    return urllib.error.HTTPError("https://opencode.ai/zen/v1/chat/completions", code, "err", headers, io.BytesIO(body))


def zen_payload(model: str = "zen/deepseek-v4-flash", **overrides) -> dict:
    payload = {
        "model": model,
        "instructions": "You are helpful.",
        "input": "hello",
        "stream": True,
    }
    payload.update(overrides)
    return payload


@pytest.fixture(autouse=True)
def _clear_secret_cache() -> None:
    clear_api_key_cache()
    yield
    clear_api_key_cache()


@pytest.fixture(autouse=True)
def _fake_zen_families(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zen_upstream, "zen_families", lambda: dict(FIXTURE_FAMILIES))
    monkeypatch.setattr(zen_upstream, "zen_model_ids", lambda: set(FIXTURE_FAMILIES))


def _meter_records() -> list[dict]:
    try:
        with open(usage_events_path(), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except FileNotFoundError:
        return []


def _fake_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zen_upstream, "resolve_api_key", lambda config, request_id: "test-key-zen")


def _request_body(req: urllib.request.Request) -> dict:
    return json.loads(req.data.decode("utf-8"))


def _request_headers(req: urllib.request.Request) -> dict[str, str]:
    return {name.lower(): value for name, value in req.headers.items()}


class TestFamilyDispatch:
    @pytest.mark.parametrize(
        ("slug", "expected_path", "auth_header"),
        [
            ("zen/claude-sonnet-4", "/messages", "x-api-key"),
            ("zen/qwen3.5-max", "/messages", "x-api-key"),
            ("zen/gemini-3-pro", ":streamGenerateContent?alt=sse", "x-goog-api-key"),
            ("zen/gpt-5.6", "/responses", "authorization"),
            ("zen/grok-4", "/responses", "authorization"),
            ("zen/deepseek-v4-flash", "/chat/completions", "authorization"),
            ("zen/glm-5", "/chat/completions", "authorization"),
        ],
    )
    def test_family_picks_endpoint_and_auth(
        self, monkeypatch: pytest.MonkeyPatch, slug: str, expected_path: str, auth_header: str
    ) -> None:
        _fake_key(monkeypatch)
        payload = zen_payload(model=slug)
        url, _body, headers = zen_upstream._build_zen_request(
            payload, zen_upstream.zen_family_for(zen_upstream.bare_zen_id(slug)),
            zen_upstream.bare_zen_id(slug), "test-key-zen", stream=True, session_model=slug,
        )
        assert expected_path in url
        assert url.startswith("https://opencode.ai/zen/v1")
        if auth_header == "authorization":
            assert headers["authorization"] == "Bearer test-key-zen"
            assert "x-api-key" not in headers
        else:
            assert headers[auth_header] == "test-key-zen"
            assert "authorization" not in headers

    def test_zen_prefix_stripped_from_upstream_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        url, body, _headers = zen_upstream._build_zen_request(
            zen_payload(), "openai_chat", "deepseek-v4-flash", "test-key-zen",
            stream=True, session_model="zen/deepseek-v4-flash",
        )
        assert body["model"] == "deepseek-v4-flash"
        assert url.endswith("/chat/completions")

    def test_unknown_id_defaults_to_chat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        family = zen_upstream.zen_family_for("brand-new-model")
        assert family == "openai_chat"

    def test_bare_slug_is_not_stripped(self) -> None:
        assert zen_upstream.bare_zen_id("deepseek-v4-flash") == "deepseek-v4-flash"
        assert zen_upstream.bare_zen_id("zen/deepseek-v4-flash") == "deepseek-v4-flash"


class TestChatFamily:
    def test_chat_payload_translation_round_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        raw = json.dumps({
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }).encode("utf-8")
        captured: list[urllib.request.Request] = []

        def fake_urlopen(req, **kw):
            captured.append(req)
            return mock.Mock(status=200, headers={}, read=lambda: raw, __enter__=lambda s: s, __exit__=lambda *a: False)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            response = zen_upstream.call_zen_responses(zen_payload(stream=False), make_config(), "req")

        assert len(captured) == 1
        sent = _request_body(captured[0])
        # Responses input translated to chat messages with the bare model.
        assert sent["model"] == "deepseek-v4-flash"
        assert sent["messages"][0] == {"role": "system", "content": "You are helpful."}
        assert sent["messages"][1] == {"role": "user", "content": "hello"}
        assert sent["stream"] is False
        assert "stream_options" not in sent
        # Translated back to a responses object.
        assert response["output_text"] == "hi"
        assert response["model"] == "zen/deepseek-v4-flash"
        assert response["usage"]["input_tokens"] == 3
        assert response["usage"]["output_tokens"] == 1

    def test_stream_request_includes_stream_options(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        captured: list[urllib.request.Request] = []
        lines = [
            b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"hi"}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n',
            b'data: [DONE]\n',
        ]

        def fake_urlopen(req, **kw):
            captured.append(req)
            return _UpstreamStream(lines)

        handler = _FakeHandler()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("opencode_go_proxy.zen_upstream.keepalive_sec", return_value=60.0):
            zen_upstream.handle_zen_responses_request(handler, zen_payload(), make_config(), "req")

        sent = _request_body(captured[0])
        assert sent["stream"] is True
        assert sent["stream_options"] == {"include_usage": True}
        events = handler.events()
        assert events[0]["type"] == "response.created"
        assert events[0]["response"]["model"] == "zen/deepseek-v4-flash"
        completed = next(e for e in events if e["type"] == "response.completed")
        assert completed["response"]["output_text"] == "hi"
        assert completed["response"]["usage"]["input_tokens"] == 1
        assert b"data: [DONE]" in handler.body()

    def test_stream_tool_calls_become_function_call_items(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        lines = [
            b'data: {"id":"1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"read_file","arguments":"{\\"path\\":"}}]}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"/tmp/x\\"}"}}]}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n',
            b'data: [DONE]\n',
        ]
        handler = _FakeHandler()
        with mock.patch("urllib.request.urlopen", return_value=_UpstreamStream(lines)), \
             mock.patch("opencode_go_proxy.zen_upstream.keepalive_sec", return_value=60.0):
            zen_upstream.handle_zen_responses_request(handler, zen_payload(), make_config(), "req")

        completed = next(e for e in handler.events() if e["type"] == "response.completed")
        calls = [item for item in completed["response"]["output"] if item["type"] == "function_call"]
        assert len(calls) == 1
        assert calls[0]["name"] == "read_file"
        assert calls[0]["arguments"] == '{"path":"/tmp/x"}'

    def test_error_envelope_surfaces_proxy_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        body = b'{"type":"error","error":{"type":"AuthError","message":"invalid key"},"metadata":{}}'
        with mock.patch("urllib.request.urlopen", side_effect=http_error(401, body=body)), \
             pytest.raises(ProxyError) as ctx:
            zen_upstream.call_zen_responses(zen_payload(stream=False), make_config(), "req")
        assert ctx.value.status == HTTPStatus.UNAUTHORIZED
        assert ctx.value.error_type == "AuthError"
        assert ctx.value.message == "invalid key"
        assert ctx.value.upstream_status == 401
        records = _meter_records()
        assert records and records[0]["provider"] == "zen"
        assert records[0]["status"] == 401

    def test_429_surfaces_retry_after(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)

        def rate_limited(req, **kw):
            raise http_error(429, retry_after="7")

        with mock.patch("urllib.request.urlopen", side_effect=rate_limited), \
             mock.patch("opencode_go_proxy.upstream.time.sleep"), \
             pytest.raises(ProxyError) as ctx:
            zen_upstream.call_zen_responses(zen_payload(stream=False), make_config(), "req")
        assert ctx.value.status == HTTPStatus.TOO_MANY_REQUESTS
        assert "retry after 7s" in ctx.value.message


class TestResponsesFamily:
    def test_verbatim_stream_relay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        lines = [
            b'data: {"type":"response.created","response":{"id":"r1","status":"in_progress"}}\n',
            b'data: {"type":"response.output_text.delta","delta":"hi"}\n',
            b'data: {"type":"response.completed","response":{"id":"r1","status":"completed"}}\n',
            b'data: [DONE]\n',
        ]
        handler = _FakeHandler()
        with mock.patch("urllib.request.urlopen", return_value=_UpstreamStream(lines)), \
             mock.patch("opencode_go_proxy.zen_upstream.keepalive_sec", return_value=60.0):
            zen_upstream.handle_zen_responses_request(handler, zen_payload(model="zen/gpt-5.6"), make_config(), "req")

        assert handler.status == 200
        assert handler.header("content-type") == "text/event-stream"
        # Relay is byte-for-byte: exactly the upstream lines, in order.
        assert handler.body() == b"".join(lines)
        records = _meter_records()
        assert records and records[0]["provider"] == "zen"
        assert records[0]["status"] == 200

    def test_verbatim_non_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        raw = json.dumps({"id": "r1", "object": "response", "status": "completed", "output_text": "hi"}).encode("utf-8")
        captured: list[urllib.request.Request] = []

        def fake_urlopen(req, **kw):
            captured.append(req)
            return mock.Mock(status=200, headers={}, read=lambda: raw, __enter__=lambda s: s, __exit__=lambda *a: False)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            response = zen_upstream.call_zen_responses(zen_payload(model="zen/gpt-5.6", stream=False), make_config(), "req")

        assert response["output_text"] == "hi"
        # The upstream sees the bare id, not the zen/ prefix.
        assert _request_body(captured[0])["model"] == "gpt-5.6"
        records = _meter_records()
        assert records and records[0]["provider"] == "zen"


class TestAnthropicFamily:
    def test_request_shape(self) -> None:
        payload = {
            "model": "zen/claude-sonnet-4",
            "instructions": "Be brief.",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": '{"path":"/tmp/x"}',
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "file contents"},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "thanks"}]},
            ],
            "max_output_tokens": 512,
            "temperature": 0.5,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file.",
                        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "name": "read_file"},
        }
        body = zen_upstream.responses_payload_to_anthropic_payload(payload)
        assert body["system"] == ["Be brief."]
        assert body["max_tokens"] == 512
        assert body["temperature"] == 0.5
        assert body["tool_choice"] == {"type": "tool", "name": "read_file"}
        assert body["tools"][0]["name"] == "read_file"
        assert body["tools"][0]["input_schema"]["properties"]["path"] == {"type": "string"}
        messages = body["messages"]
        assert messages[0]["role"] == "user"
        assert messages[0]["content"][0] == {"type": "text", "text": "hi"}
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"][0] == {
            "type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "/tmp/x"},
        }
        assert messages[2]["role"] == "user"
        assert messages[2]["content"][0]["type"] == "tool_result"
        assert messages[2]["content"][0]["tool_use_id"] == "call_1"
        assert messages[2]["content"][0]["content"] == "file contents"

    def test_stream_back_translation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        lines = [
            b'event: message_start\n',
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":4}}}\n',
            b'\n',
            b'event: content_block_start\n',
            b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n',
            b'\n',
            b'event: content_block_delta\n',
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hel"}}\n',
            b'\n',
            b'event: content_block_delta\n',
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo"}}\n',
            b'\n',
            b'event: content_block_stop\n',
            b'data: {"type":"content_block_stop","index":0}\n',
            b'\n',
            b'event: content_block_start\n',
            b'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","id":"tu_1","name":"read_file"}}\n',
            b'\n',
            b'event: content_block_delta\n',
            b'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{\\"path\\":"}}\n',
            b'\n',
            b'event: content_block_delta\n',
            b'data: {"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"\\"/tmp/x\\"}"}}\n',
            b'\n',
            b'event: content_block_stop\n',
            b'data: {"type":"content_block_stop","index":1}\n',
            b'\n',
            b'event: message_delta\n',
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":7}}\n',
            b'\n',
            b'event: message_stop\n',
            b'data: {"type":"message_stop"}\n',
            b'\n',
        ]
        captured: list[urllib.request.Request] = []
        handler = _FakeHandler()

        def fake_urlopen(req, **kw):
            captured.append(req)
            return _UpstreamStream(lines)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("opencode_go_proxy.zen_upstream.keepalive_sec", return_value=60.0):
            zen_upstream.handle_zen_responses_request(handler, zen_payload(model="zen/claude-sonnet-4"), make_config(), "req")

        assert handler.status == 200
        headers = _request_headers(captured[0])
        assert headers["x-api-key"] == "test-key-zen"
        assert headers["anthropic-version"] == "2023-06-01"
        events = handler.events()
        deltas = [e["delta"] for e in events if e["type"] == "response.output_text.delta"]
        assert deltas == ["Hel", "lo"]
        completed = next(e for e in events if e["type"] == "response.completed")
        assert completed["response"]["output_text"] == "Hello"
        assert completed["response"]["usage"]["input_tokens"] == 4
        assert completed["response"]["usage"]["output_tokens"] == 7
        calls = [item for item in completed["response"]["output"] if item["type"] == "function_call"]
        assert len(calls) == 1
        assert calls[0]["name"] == "read_file"
        assert calls[0]["arguments"] == '{"path":"/tmp/x"}'

    def test_non_stream_translation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        raw = json.dumps({
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Sure"},
                {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "/tmp/x"}},
            ],
            "usage": {"input_tokens": 4, "output_tokens": 7},
        }).encode("utf-8")

        def fake_urlopen(req, **kw):
            return mock.Mock(status=200, headers={}, read=lambda: raw, __enter__=lambda s: s, __exit__=lambda *a: False)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            response = zen_upstream.call_zen_responses(
                zen_payload(model="zen/claude-sonnet-4", stream=False), make_config(), "req"
            )
        assert response["output_text"] == "Sure"
        calls = [item for item in response["output"] if item["type"] == "function_call"]
        assert len(calls) == 1
        assert calls[0]["name"] == "read_file"
        assert calls[0]["arguments"] == '{"path":"/tmp/x"}'
        assert response["usage"]["input_tokens"] == 4


class TestGeminiFamily:
    def test_request_shape(self) -> None:
        payload = {
            "model": "zen/gemini-3-pro",
            "instructions": "Be concise.",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": '{"path":"/tmp/x"}'},
                {"type": "function_call_output", "call_id": "call_1", "output": "file contents"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a file.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        }
        body = zen_upstream.responses_payload_to_gemini_payload(payload)
        assert body["systemInstruction"] == {"parts": [{"text": "Be concise."}]}
        assert body["contents"][0] == {"role": "user", "parts": [{"text": "hi"}]}
        # Assistant tool call -> functionCall part.
        assert body["contents"][1]["role"] == "model"
        assert body["contents"][1]["parts"] == [{"functionCall": {"name": "read_file", "args": {"path": "/tmp/x"}}}]
        # Tool result -> user functionResponse part.
        assert body["contents"][2] == {
            "role": "user",
            "parts": [{"functionResponse": {"name": "read_file", "response": {"value": "file contents"}}}],
        }
        assert body["tools"][0]["functionDeclarations"][0]["name"] == "read_file"

    def test_stream_back_translation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        lines = [
            b'data: {"candidates":[{"content":{"parts":[{"text":"Hi "}]},"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":3,"candidatesTokenCount":1,"totalTokenCount":4}}\n\n',
            b'data: {"candidates":[{"content":{"parts":[{"text":"there"}]}}]}\n\n',
            b'data: {"candidates":[{"content":{"parts":[{"functionCall":{"name":"read_file","args":{"path":"/tmp/x"}}}]}}],"usageMetadata":{"promptTokenCount":3,"candidatesTokenCount":4,"totalTokenCount":7}}\n\n',
        ]
        captured: list[urllib.request.Request] = []
        handler = _FakeHandler()

        def fake_urlopen(req, **kw):
            captured.append(req)
            return _UpstreamStream(lines)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("opencode_go_proxy.zen_upstream.keepalive_sec", return_value=60.0):
            zen_upstream.handle_zen_responses_request(handler, zen_payload(model="zen/gemini-3-pro"), make_config(), "req")

        assert handler.status == 200
        assert _request_headers(captured[0])["x-goog-api-key"] == "test-key-zen"
        url = captured[0].full_url
        assert url.endswith("/models/gemini-3-pro:streamGenerateContent?alt=sse")
        events = handler.events()
        deltas = [e["delta"] for e in events if e["type"] == "response.output_text.delta"]
        assert deltas == ["Hi ", "there"]
        completed = next(e for e in events if e["type"] == "response.completed")
        assert completed["response"]["output_text"] == "Hi there"
        assert completed["response"]["usage"]["input_tokens"] == 3
        assert completed["response"]["usage"]["output_tokens"] == 4
        calls = [item for item in completed["response"]["output"] if item["type"] == "function_call"]
        assert len(calls) == 1
        assert calls[0]["name"] == "read_file"
        assert calls[0]["arguments"] == '{"path":"/tmp/x"}'

    def test_non_stream_translation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        raw = json.dumps({
            "candidates": [{"content": {"parts": [{"text": "Hi there"}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2, "totalTokenCount": 5},
        }).encode("utf-8")

        def fake_urlopen(req, **kw):
            return mock.Mock(status=200, headers={}, read=lambda: raw, __enter__=lambda s: s, __exit__=lambda *a: False)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            response = zen_upstream.call_zen_responses(
                zen_payload(model="zen/gemini-3-pro", stream=False), make_config(), "req"
            )
        assert response["output_text"] == "Hi there"
        assert response["usage"]["total_tokens"] == 5


class TestStreamErrorHandling:
    def test_stream_upstream_error_relayed_with_retry_after(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        body = b'{"type":"error","error":{"type":"RateLimit","message":"slow down"},"metadata":{}}'
        handler = _FakeHandler()

        def rate_limited(req, **kw):
            raise http_error(429, body=body, retry_after="3")

        with mock.patch("urllib.request.urlopen", side_effect=rate_limited), \
             mock.patch("opencode_go_proxy.streaming.time.sleep"):
            zen_upstream.handle_zen_responses_request(handler, zen_payload(), make_config(), "req")

        assert handler.status == 429
        assert handler.header("retry-after") == "3"
        assert handler.header("content-type") == "application/json"
        assert handler.body() == body
        # No SSE events were synthesized (connect-first).
        assert handler.events() == []
        records = _meter_records()
        assert records and records[0]["provider"] == "zen"
        assert records[0]["status"] == 429

    def test_stream_network_error_raises_proxy_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")), \
             pytest.raises(ProxyError) as ctx:
            zen_upstream.handle_zen_responses_request(_FakeHandler(), zen_payload(), make_config(), "req")
        assert ctx.value.status == HTTPStatus.BAD_GATEWAY
        records = _meter_records()
        assert records and records[0]["provider"] == "zen"
        assert records[0]["status"] == 502

    def test_stream_aborted_after_200_meters_502(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        # Upstream opens the stream then dies mid-body (IncompleteRead).
        class _AbortStream(_UpstreamStream):
            def __exit__(self, *args: object) -> bool:
                raise http.client.IncompleteRead(b"partial")

        lines = [b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"partial"}}]}\n']
        handler = _FakeHandler()
        with mock.patch("urllib.request.urlopen", return_value=_AbortStream(lines)):
            zen_upstream.handle_zen_responses_request(handler, zen_payload(), make_config(), "req")
        assert handler.status == 200  # SSE head was committed
        records = _meter_records()
        assert records and records[0]["provider"] == "zen"
        assert records[0]["status"] == 502
        assert records[0]["streamAborted"] is True

    def test_stream_empty_completion_metered_as_502(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        # Two empty 200s (the retry also empty) end as response.error
        # empty_completion; the client-visible outcome is a failed turn, so
        # the meter records 502 with the emptyCompletion marker, never 200.
        lines = [
            b'data: {"id":"1","choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n',
            b'data: [DONE]\n',
        ]
        handler = _FakeHandler()
        with mock.patch("urllib.request.urlopen", return_value=_UpstreamStream(lines)):
            zen_upstream.handle_zen_responses_request(handler, zen_payload(), make_config(), "req")
        assert handler.status == 200  # SSE head was committed
        events = handler.events()
        errors = [e for e in events if e.get("type") == "response.error"]
        assert len(errors) == 1
        assert errors[0]["error"]["code"] == "empty_completion"
        assert not any(e.get("type") == "response.completed" for e in events)
        records = _meter_records()
        assert records and records[0]["provider"] == "zen"
        assert records[0]["status"] == 502
        assert records[0]["emptyCompletion"] is True


class TestZenChatSurface:
    def test_stream_relay_to_zen_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        lines = [
            b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"hi"}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n',
            b'data: [DONE]\n',
        ]
        captured: list[urllib.request.Request] = []
        handler = _FakeHandler()

        def fake_urlopen(req, **kw):
            captured.append(req)
            return _UpstreamStream(lines)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch("opencode_go_proxy.zen_upstream.keepalive_sec", return_value=60.0):
            zen_upstream.handle_zen_chat_request(
                handler,
                {"model": "zen/deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}], "stream": True},
                make_config(), "req",
            )

        assert handler.status == 200
        assert captured[0].full_url.endswith("/chat/completions")
        assert captured[0].full_url.startswith("https://opencode.ai/zen/v1")
        assert _request_body(captured[0])["model"] == "deepseek-v4-flash"
        assert handler.body() == b"".join(lines)
        records = _meter_records()
        assert records and records[0]["provider"] == "zen"

    def test_non_stream_relays_status_and_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_key(monkeypatch)
        raw = b'{"choices":[{"message":{"content":"hi"}}]}'
        handler = _FakeHandler()
        with mock.patch("urllib.request.urlopen", return_value=mock.Mock(
            status=200, headers={}, read=lambda: raw, __enter__=lambda s: s, __exit__=lambda *a: False
        )):
            zen_upstream.handle_zen_chat_request(
                handler,
                {"model": "zen/deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}]},
                make_config(), "req",
            )
        assert handler.status == 200
        assert handler.body() == raw


class _FakeZenUpstream(BaseHTTPRequestHandler):
    """Local fake zen /chat/completions + /messages + /responses upstream."""

    mode = "chat-sse"
    captured: ClassVar[list[dict]] = []

    def _emit(self, status: int, content_type: str, payload: bytes, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        type(self).captured.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if type(self).mode == "chat-sse":
            payload = (
                b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"zen-answer"}}]}\n'
                b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n'
                b'data: [DONE]\n'
            )
            self._emit(200, "text/event-stream", payload)
            return
        self._emit(200, "application/json", b'{"choices":[]}')


class TestAppDispatch:
    def test_zen_request_never_reaches_go_path(self, tmp_path) -> None:
        """End to end: a zen-routed /v1/responses stream is served by the zen
        client and the opencode-go upstream sees zero traffic."""
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        from opencode_go_proxy.app import ResponsesProxyHandler

        go_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeZenUpstream)
        zen_server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeZenUpstream)
        go_thread = threading.Thread(target=go_server.serve_forever, daemon=True)
        zen_thread = threading.Thread(target=zen_server.serve_forever, daemon=True)
        go_thread.start()
        zen_thread.start()
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesProxyHandler)
            server.config = make_config()  # type: ignore[attr-defined]
            server.config.chat_base_url = f"http://127.0.0.1:{go_server.server_port}/v1"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            _FakeZenUpstream.mode = "chat-sse"
            _FakeZenUpstream.captured = []

            env = {
                "OPENCODE_GO_PROXY_STATE_DIR": str(state),
                "OPENCODE_ZEN_BASE_URL": f"http://127.0.0.1:{zen_server.server_port}/zen/v1",
            }
            with mock.patch.dict(os.environ, env), \
                 mock.patch("opencode_go_proxy.zen_upstream.resolve_api_key", return_value="test-key-zen"), \
                 mock.patch("opencode_go_proxy.zen_upstream.keepalive_sec", return_value=60.0):
                import http.client

                conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                payload = json.dumps({
                    "model": "zen/deepseek-v4-flash",
                    "input": "hello",
                    "stream": True,
                })
                conn.request("POST", "/v1/responses", body=payload, headers={"content-type": "application/json"})
                resp = conn.getresponse()
                streamed = resp.read().decode("utf-8")
                conn.close()

            assert resp.status == 200
            assert "zen-answer" in streamed
            assert "response.completed" in streamed
            # The zen upstream received the translated chat request; the
            # opencode-go upstream (same fake class, different port) saw none.
            zen_host = f"127.0.0.1:{zen_server.server_port}"
            go_host = f"127.0.0.1:{go_server.server_port}"
            zen_records = [e for e in _FakeZenUpstream.captured if e["headers"].get("Host") == zen_host]
            go_records = [e for e in _FakeZenUpstream.captured if e["headers"].get("Host") == go_host]
            assert zen_records, "zen upstream should have received the request"
            assert not go_records, "opencode-go upstream must never see a zen request"
            assert zen_records[0]["path"] == "/zen/v1/chat/completions"
            sent = json.loads(zen_records[0]["body"])
            assert sent["model"] == "deepseek-v4-flash"

            records = []
            try:
                with open(state / "usage-events.jsonl", encoding="utf-8") as handle:
                    records = [json.loads(line) for line in handle if line.strip()]
            except FileNotFoundError:
                pass
            assert records and records[0]["provider"] == "zen"
            assert records[0]["status"] == 200
        finally:
            server.shutdown()
            server.server_close()
            go_server.shutdown()
            go_server.server_close()
            zen_server.shutdown()
            zen_server.server_close()
