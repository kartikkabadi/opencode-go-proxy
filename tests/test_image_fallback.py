"""Runtime image fallback for image turns the upstream rejects (plan 009b).

A non-tools image turn routes to the requested model when the catalog marks it
image-capable. If the upstream then rejects the image payload at runtime with a
caption-fallback 4xx (400/404/415/422), the proxy captions the images through
the vision module and retries the same requested model once instead of failing
the turn. The split-turn (image + tools) path captions pre-flight, so its
rejections are never re-captioned.
"""

import io
import json
import os
import urllib.error
from http import HTTPStatus
from unittest import mock

import pytest

from opencode_go_proxy.app import ProxyConfig, handle_responses_request
from opencode_go_proxy.errors import ProxyError
from opencode_go_proxy.meter import usage_events_path
from opencode_go_proxy.streaming import handle_streaming_request
from opencode_go_proxy.vision import is_image_rejection_status

IMAGE_URL = "data:image/png;base64,iVBORw0KGgo="


def make_config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=8787,
        chat_base_url="https://up.test/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=10,
        max_body_bytes=1024 * 1024,
    )


def image_payload(model: str, *, tools: bool = False) -> dict:
    item = {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "What is on screen?"},
            {"type": "input_image", "image_url": IMAGE_URL},
        ],
    }
    payload: dict = {"model": model, "input": [item]}
    if tools:
        payload["tools"] = [
            {
                "type": "function",
                "name": "read_file",
                "description": "read a file",
                "parameters": {"type": "object", "properties": {}},
            }
        ]
    return payload


def text_payload(model: str = "deepseek-v4-flash") -> dict:
    return {"model": model, "input": "hello"}


def _ok_chat(text: str = "ok") -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


def _reject(status: int) -> ProxyError:
    return ProxyError(HTTPStatus.BAD_GATEWAY, f"upstream HTTP {status}", upstream_status=status)


def _has_image_part(messages) -> bool:
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                return True
    return False


def _caption(payload, target_model, config, request_id) -> dict:
    """Stub of caption_images_in_messages: images become a text placeholder."""
    payload = dict(payload)
    payload["model"] = target_model
    messages = []
    for message in payload["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            message = dict(message)
            message["content"] = [
                {"type": "text", "text": "[screenshot: fixture]"}
                if isinstance(part, dict) and part.get("type") == "image_url"
                else part
                for part in content
            ]
        messages.append(message)
    payload["messages"] = messages
    return payload


def _meter_events() -> list[dict]:
    try:
        with open(usage_events_path(), encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


class TestImageRejectionStatus:
    def test_caption_fallback_statuses_only(self) -> None:
        assert is_image_rejection_status(400)
        assert is_image_rejection_status(404)
        assert is_image_rejection_status(415)
        assert is_image_rejection_status(422)
        assert not is_image_rejection_status(401)
        assert not is_image_rejection_status(429)
        assert not is_image_rejection_status(500)
        assert not is_image_rejection_status(None)


class TestNonStreamImageFallback:
    def test_runtime_rejection_retries_through_caption_path(self) -> None:
        calls: list[dict] = []

        def fake_upstream(payload, config, request_id, **kwargs):
            calls.append(payload)
            if len(calls) == 1:
                raise _reject(400)
            return _ok_chat(), 0

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat", side_effect=fake_upstream
        ), mock.patch(
            "opencode_go_proxy.app.caption_images_in_messages", side_effect=_caption
        ) as caption:
            response = handle_responses_request(image_payload("deepseek-v4-flash"), make_config(), "req")

        assert len(calls) == 2
        assert calls[0]["model"] == "deepseek-v4-flash"
        assert _has_image_part(calls[0]["messages"])
        assert calls[1]["model"] == "deepseek-v4-flash"
        assert not _has_image_part(calls[1]["messages"])
        caption.assert_called_once()
        assert response["model"] == "deepseek-v4-flash"
        assert response["output_text"] == "ok"
        meter = _meter_events()
        assert len(meter) == 1
        assert meter[0]["status"] == 200
        assert meter[0]["retries"] == 1

    def test_caption_retry_rejection_surfaces_error(self) -> None:
        def fake_upstream(payload, config, request_id, **kwargs):
            raise _reject(404)

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat", side_effect=fake_upstream
        ), mock.patch(
            "opencode_go_proxy.app.caption_images_in_messages", side_effect=_caption
        ) as caption, pytest.raises(ProxyError):
            handle_responses_request(image_payload("deepseek-v4-flash"), make_config(), "req")

        caption.assert_called_once()

    def test_non_image_rejection_is_not_rerouted(self) -> None:
        def fake_upstream(payload, config, request_id, **kwargs):
            raise _reject(400)

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat", side_effect=fake_upstream
        ), mock.patch("opencode_go_proxy.app.caption_images_in_messages") as caption, pytest.raises(ProxyError):
            handle_responses_request(text_payload(), make_config(), "req")

        caption.assert_not_called()

    def test_tools_image_turn_rejection_never_recaptions(self) -> None:
        # The split-turn path captions before the first upstream call, so a
        # rejection there is not an image-modality problem and must not spawn a
        # second caption pass.
        def fake_upstream(payload, config, request_id, **kwargs):
            raise _reject(400)

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat", side_effect=fake_upstream
        ), mock.patch(
            "opencode_go_proxy.app.caption_images_in_messages", side_effect=_caption
        ) as caption, pytest.raises(ProxyError):
            handle_responses_request(image_payload("deepseek-v4-flash", tools=True), make_config(), "req")

        caption.assert_called_once()

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


def _stream_request(payload: dict, urlopen) -> list[dict]:
    wfile = io.BytesIO()
    with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
        "urllib.request.urlopen", urlopen
    ):
        handle_streaming_request(payload, make_config(), "req", wfile)
    events: list[dict] = []
    for block in wfile.getvalue().decode("utf-8").split("\n\n"):
        block = block.strip()
        if not block.startswith("data: "):
            continue
        data = block[len("data: "):]
        if data == "[DONE]":
            continue
        events.append(json.loads(data))
    return events


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://up.test/v1/chat/completions",
        status,
        "rejected",
        {},
        io.BytesIO(b'{"error":"rejected"}'),
    )


class TestStreamImageFallback:
    def test_stream_rejection_retries_through_caption_path(self) -> None:
        lines = [
            b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"hi"}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n',
            b"data: [DONE]\n",
        ]
        requests: list = []

        def fake_urlopen(req, **kwargs):
            requests.append(req)
            if len(requests) == 1:
                raise _http_error(400)
            return _UpstreamStream(lines)

        with mock.patch(
            "opencode_go_proxy.streaming.caption_images_in_messages", side_effect=_caption
        ) as caption:
            events = _stream_request(image_payload("deepseek-v4-flash"), fake_urlopen)

        assert len(requests) == 2
        retry_body = json.loads(requests[1].data)
        retry_text = json.dumps(retry_body)
        assert "image_url" not in retry_text
        assert not any(e["type"] == "response.error" for e in events)
        completed = next(e for e in events if e["type"] == "response.completed")["response"]
        assert completed["output_text"] == "hi"
        caption.assert_called_once()
        meter = _meter_events()
        assert len(meter) == 1
        assert meter[0]["status"] == 200
        assert meter[0]["retries"] == 1

    def test_stream_captioned_retry_rejection_surfaces_error(self) -> None:
        def fake_urlopen(req, **kwargs):
            raise _http_error(400)

        with mock.patch(
            "opencode_go_proxy.streaming.caption_images_in_messages", side_effect=_caption
        ) as caption:
            events = _stream_request(image_payload("deepseek-v4-flash"), fake_urlopen)

        assert any(e["type"] == "response.error" for e in events)
        caption.assert_called_once()
        meter = _meter_events()
        assert len(meter) == 1
        assert meter[0]["status"] == 400
        assert meter[0]["retries"] == 1
