"""Go 401 ModelError fallback: bare slugs the go gateway advertises but does
not serve re-dispatch to zen (plan 010).

A bare slug that exists in BOTH the go catalog and the zen catalog routes to
the opencode-go path (go wins for bare collisions). When the go gateway
rejects that specific slug with its own ModelError "not supported" envelope
(401/403), the proxy re-dispatches the identical request to the zen path
instead of surfacing the go rejection — non-stream responses, non-stream
chat, and both streaming paths (the responses stream hands the open SSE
stream to the zen relay; the chat stream hands the request to the zen chat
handler before anything is committed). Every other go answer — 200, an
invalid-key 401, a prefixed slug, a slug zen does not own — is handled
exactly as before.
"""

import io
import json
import os
import urllib.error
from http import HTTPStatus
from unittest import mock

import pytest

from opencode_go_proxy import catalog, routing, zen_catalog
from opencode_go_proxy.app import (
    ProxyConfig,
    _go_reject_zen_fallback,
    handle_chat_completions_request,
    handle_responses_request,
)
from opencode_go_proxy.errors import ProxyError
from opencode_go_proxy.meter import state_dir, usage_events_path
from opencode_go_proxy.secrets import clear_api_key_cache
from opencode_go_proxy.streaming import (
    _ConnectFailed,
    handle_chat_stream_passthrough,
    handle_streaming_request,
)

ZEN_SLUG = "north-mini-code-free"

# The shape seen live from the go gateway: ModelError with "not supported".
GO_REJECT_BODY = json.dumps(
    {
        "type": "error",
        "error": {"type": "ModelError", "message": f"Model {ZEN_SLUG} is not supported"},
    }
)

INVALID_KEY_BODY = json.dumps(
    {"error": {"type": "authentication_error", "message": "invalid api key"}}
)


def make_config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=8787,
        chat_base_url="https://up.test/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=10,
        max_body_bytes=1024 * 1024,
    )


def _seed_collision(slug: str = ZEN_SLUG) -> None:
    """Seed the isolated state dir so `slug` sits in BOTH catalogs.

    That is the collision case the fallback exists for: routing keeps the bare
    slug on the opencode-go path (go wins for bare collisions) even though the
    zen capture also owns it, and the helper sees the slug in zen_model_ids().
    """
    state = state_dir()
    os.makedirs(state, exist_ok=True)
    with open(zen_catalog.zen_models_path(), "w") as handle:
        json.dump(
            {"fetched_at": "2026-08-14T00:00:00Z", "models": [{"id": slug}]},
            handle,
        )
    with open(catalog.state_compact_path(), "w") as handle:
        json.dump(
            {
                "fetched_at": "2026-08-14T00:00:00Z",
                "etag": "",
                "client_version": "0.0.0",
                "models": [{"slug": slug}],
            },
            handle,
        )
    zen_catalog._ZEN_MODELS_CACHE = None
    routing._GO_COMPACT_CACHE = None


def _go_reject(status: int = 401, body: str = GO_REJECT_BODY) -> ProxyError:
    return ProxyError(
        HTTPStatus.BAD_GATEWAY,
        f"upstream HTTP {status}",
        upstream_status=status,
        body=body,
    )


def _ok_chat(text: str = "ok") -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }


def _zen_ok() -> dict:
    return {
        "id": "resp_zen",
        "object": "response",
        "status": "completed",
        "model": ZEN_SLUG,
        "output": [{"type": "message", "id": "msg_1", "role": "assistant",
                    "status": "completed", "content": [{"type": "output_text", "text": "zen ok"}]}],
        "output_text": "zen ok",
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }


def responses_payload(model: str = ZEN_SLUG, *, stream: bool | None = None) -> dict:
    payload: dict = {"model": model, "input": "hello"}
    if stream is not None:
        payload["stream"] = stream
    return payload


class _FakeHandler:
    """Minimal ResponsesProxyHandler double for the chat relay assertions."""

    def __init__(self) -> None:
        self.wfile = io.BytesIO()
        self.status: int | None = None
        self.headers_list: list[tuple[str, str]] = []

    def send_response(self, status) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.headers_list.append((name, value))

    def end_headers(self) -> None:
        pass

    def flush(self) -> None:
        pass


class _UpstreamStream:
    """Fake urllib response iterating prebuilt SSE lines."""

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


class _AbortingStream(_UpstreamStream):
    """Fake upstream that dies mid-stream after one data line."""

    def __init__(self) -> None:
        super().__init__([])

    def __iter__(self):
        yield b'data: {"id":"z1","choices":[{"index":0,"delta":{"content":"zen hi"}}]}\n'
        raise OSError("connection reset")


def _go_http_error(status: int = 401, body: str = GO_REJECT_BODY) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://up.test/v1/chat/completions", status, "upstream error", {}, io.BytesIO(body.encode())
    )


def _sse_events(wfile: io.BytesIO) -> list[dict]:
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


def _meter_events() -> list[dict]:
    try:
        with open(usage_events_path(), encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
    except FileNotFoundError:
        return []


def _zen_stream_lines() -> list[bytes]:
    return [
        b'data: {"id":"z1","choices":[{"index":0,"delta":{"content":"zen hi"}}]}\n',
        b'data: {"id":"z1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n',
        b"data: [DONE]\n",
    ]


class TestGoRejectHelper:
    def test_accepts_live_401_shape(self) -> None:
        _seed_collision()
        assert _go_reject_zen_fallback(ZEN_SLUG, 401, GO_REJECT_BODY)

    def test_accepts_403_with_whitespace_tolerance(self) -> None:
        _seed_collision()
        sloppy = '{"error": {"type": "ModelError", "message": "Model  north-mini-code-free\\n  is not supported"}}'
        assert _go_reject_zen_fallback(ZEN_SLUG, 403, sloppy)

    def test_rejects_non_model_error_type(self) -> None:
        _seed_collision()
        assert not _go_reject_zen_fallback(ZEN_SLUG, 401, INVALID_KEY_BODY)

    def test_rejects_wrong_status(self) -> None:
        _seed_collision()
        assert not _go_reject_zen_fallback(ZEN_SLUG, 500, GO_REJECT_BODY)

    def test_rejects_prefixed_slug(self) -> None:
        _seed_collision()
        assert not _go_reject_zen_fallback(f"opencode-go/{ZEN_SLUG}", 401, GO_REJECT_BODY)
        assert not _go_reject_zen_fallback(f"zen/{ZEN_SLUG}", 401, GO_REJECT_BODY)

    def test_rejects_slug_not_in_zen_catalog(self) -> None:
        assert not _go_reject_zen_fallback(ZEN_SLUG, 401, GO_REJECT_BODY)

    def test_rejects_unparseable_body(self) -> None:
        _seed_collision()
        assert not _go_reject_zen_fallback(ZEN_SLUG, 401, "not json")


class TestResponsesFallback:
    def test_go_reject_falls_back_to_zen_with_identical_body(self) -> None:
        _seed_collision()
        payload = responses_payload()
        config = make_config()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat", side_effect=_go_reject(401)
        ) as go, mock.patch(
            "opencode_go_proxy.app.call_zen_responses", return_value=_zen_ok()
        ) as zen:
            response = handle_responses_request(payload, config, "req")

        go.assert_called_once()
        zen.assert_called_once()
        assert zen.call_args.args[0] == payload  # identical body + original slug
        assert zen.call_args.args[1] is config
        assert zen.call_args.args[2] == "req"
        assert response == _zen_ok()

    def test_stream_flag_preserved_across_fallback(self) -> None:
        _seed_collision()
        payload = responses_payload(stream=False)

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat", side_effect=_go_reject(401)
        ), mock.patch(
            "opencode_go_proxy.app.call_zen_responses", return_value=_zen_ok()
        ) as zen:
            handle_responses_request(payload, make_config(), "req")

        called = zen.call_args.args[0]
        assert called["stream"] is False
        assert called == payload

    def test_go_401_invalid_key_does_not_fallback(self) -> None:
        _seed_collision()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat", side_effect=_go_reject(401, INVALID_KEY_BODY)
        ), mock.patch("opencode_go_proxy.app.call_zen_responses") as zen, pytest.raises(ProxyError) as ctx:
            handle_responses_request(responses_payload(), make_config(), "req")

        zen.assert_not_called()
        assert ctx.value.upstream_status == 401
        assert ctx.value.message == "upstream HTTP 401"

    def test_go_200_no_fallback(self) -> None:
        _seed_collision()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat", return_value=(_ok_chat(), 0)
        ), mock.patch("opencode_go_proxy.app.call_zen_responses") as zen:
            response = handle_responses_request(responses_payload(), make_config(), "req")

        zen.assert_not_called()
        assert response["output_text"] == "ok"

    def test_bare_slug_not_in_zen_ids_no_fallback(self) -> None:
        # Seed only the go side: routing still lands on opencode-go, but the
        # slug is not in zen_model_ids(), so the rejection stays a go error.
        _seed_collision()
        from opencode_go_proxy import zen_catalog as _zc

        _zc._ZEN_MODELS_CACHE = None
        with open(_zc.zen_models_path(), "w") as handle:
            json.dump({"fetched_at": "2026-08-14T00:00:00Z", "models": []}, handle)

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat", side_effect=_go_reject(401)
        ), mock.patch("opencode_go_proxy.app.call_zen_responses") as zen, pytest.raises(ProxyError) as ctx:
            handle_responses_request(responses_payload(), make_config(), "req")

        zen.assert_not_called()
        assert ctx.value.upstream_status == 401

    def test_prefixed_opencode_go_slug_no_fallback(self) -> None:
        _seed_collision()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat", side_effect=_go_reject(401)
        ), mock.patch("opencode_go_proxy.app.call_zen_responses") as zen, pytest.raises(ProxyError) as ctx:
            handle_responses_request(responses_payload(f"opencode-go/{ZEN_SLUG}"), make_config(), "req")

        zen.assert_not_called()
        assert ctx.value.upstream_status == 401

    def test_zen_attempt_error_surfaces_zen_envelope(self) -> None:
        _seed_collision()

        def zen_fails(payload, config, request_id):
            raise ProxyError(
                HTTPStatus.BAD_GATEWAY,
                "zen upstream HTTP 400",
                upstream_status=400,
                error_type="ZenModelError",
            )

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat", side_effect=_go_reject(401)
        ), mock.patch("opencode_go_proxy.app.call_zen_responses", side_effect=zen_fails), pytest.raises(ProxyError) as ctx:
            handle_responses_request(responses_payload(), make_config(), "req")

        # The zen error (more actionable for a zen-served model), never the
        # go 401 the fallback superseded.
        assert ctx.value.message == "zen upstream HTTP 400"
        assert ctx.value.error_type == "ZenModelError"
        assert ctx.value.upstream_status == 400


def chat_payload(model: str = ZEN_SLUG) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
    }


class TestChatFallback:
    def test_go_reject_falls_back_to_zen_with_identical_messages(self) -> None:
        _seed_collision()
        payload = chat_payload()
        config = make_config()
        handler = _FakeHandler()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat_verbatim",
            return_value=(401, GO_REJECT_BODY.encode(), 0, "application/json", None),
        ), mock.patch("opencode_go_proxy.app.handle_zen_chat_request") as zen:
            handle_chat_completions_request(handler, payload, config, "req")

        zen.assert_called_once()
        called_handler, zen_payload, called_config, called_request_id = zen.call_args.args
        assert called_handler is handler
        assert called_config is config
        assert called_request_id == "req"
        # The zen chat handler's API takes the zen/ slug; the wire request it
        # sends strips it back to the bare id with identical messages/stream.
        assert zen_payload["model"] == f"zen/{ZEN_SLUG}"
        assert zen_payload["messages"] == payload["messages"]
        assert zen_payload["stream"] is payload["stream"]

    def test_go_401_invalid_key_relayed_verbatim_no_fallback(self) -> None:
        _seed_collision()
        handler = _FakeHandler()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat_verbatim",
            return_value=(401, INVALID_KEY_BODY.encode(), 0, "application/json", None),
        ), mock.patch("opencode_go_proxy.app.handle_zen_chat_request") as zen:
            handle_chat_completions_request(handler, chat_payload(), make_config(), "req")

        zen.assert_not_called()
        assert handler.status == 401
        assert handler.wfile.getvalue() == INVALID_KEY_BODY.encode()

    def test_go_200_relayed_no_fallback(self) -> None:
        _seed_collision()
        ok_body = b'{"choices": [], "usage": {}}'
        handler = _FakeHandler()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat_verbatim",
            return_value=(200, ok_body, 0, "application/json", None),
        ), mock.patch("opencode_go_proxy.app.handle_zen_chat_request") as zen:
            handle_chat_completions_request(handler, chat_payload(), make_config(), "req")

        zen.assert_not_called()
        assert handler.status == 200
        assert handler.wfile.getvalue() == ok_body

    def test_bare_slug_not_in_zen_ids_no_fallback(self) -> None:
        from opencode_go_proxy import zen_catalog as _zc

        _zc._ZEN_MODELS_CACHE = None
        with open(_zc.zen_models_path(), "w") as handle:
            json.dump({"fetched_at": "2026-08-14T00:00:00Z", "models": []}, handle)
        handler = _FakeHandler()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat_verbatim",
            return_value=(401, GO_REJECT_BODY.encode(), 0, "application/json", None),
        ), mock.patch("opencode_go_proxy.app.handle_zen_chat_request") as zen:
            handle_chat_completions_request(handler, chat_payload(), make_config(), "req")

        zen.assert_not_called()
        assert handler.status == 401

    def test_prefixed_opencode_go_slug_no_fallback(self) -> None:
        _seed_collision()
        handler = _FakeHandler()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat_verbatim",
            return_value=(401, GO_REJECT_BODY.encode(), 0, "application/json", None),
        ), mock.patch("opencode_go_proxy.app.handle_zen_chat_request") as zen:
            handle_chat_completions_request(handler, chat_payload(f"opencode-go/{ZEN_SLUG}"), make_config(), "req")

        zen.assert_not_called()
        assert handler.status == 401

    def test_zen_attempt_error_propagates(self) -> None:
        _seed_collision()
        handler = _FakeHandler()

        def zen_fails(handler_arg, payload, config, request_id):
            raise ProxyError(
                HTTPStatus.BAD_GATEWAY,
                "zen upstream network error: boom",
                upstream_status=None,
                error_type="ZenNetworkError",
            )

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.app.call_upstream_chat_verbatim",
            return_value=(401, GO_REJECT_BODY.encode(), 0, "application/json", None),
        ), mock.patch("opencode_go_proxy.app.handle_zen_chat_request", side_effect=zen_fails), pytest.raises(ProxyError) as ctx:
            handle_chat_completions_request(handler, chat_payload(), make_config(), "req")

        assert ctx.value.message == "zen upstream network error: boom"
        assert ctx.value.error_type == "ZenNetworkError"


class TestStreamingFallback:
    """Stream=true responses: the go-reject zen fallback relays the zen stream.

    The SSE head is committed before the go upstream connects, so on the
    rejection the fallback hands the already-open SSE stream to the zen relay:
    the client sees the zen relay's own response.created, the zen events, and
    a provider="zen" meter record with the zen outcome.
    """

    def test_go_reject_falls_back_to_zen_stream(self) -> None:
        _seed_collision()
        clear_api_key_cache()
        wfile = io.BytesIO()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "urllib.request.urlopen",
            side_effect=[_go_http_error(), _UpstreamStream(_zen_stream_lines())],
        ):
            handle_streaming_request(responses_payload(stream=True), make_config(), "req", wfile)

        events = _sse_events(wfile)
        # One created, from the zen relay (the go path never emitted one), with
        # the original bare slug; then the zen stream, no error terminal.
        created = [e for e in events if e["type"] == "response.created"]
        assert len(created) == 1
        assert created[0]["response"]["model"] == ZEN_SLUG
        completed = [e for e in events if e["type"] == "response.completed"]
        assert len(completed) == 1
        assert completed[0]["response"]["output_text"] == "zen hi"
        deltas = [e for e in events if e["type"] == "response.output_text.delta"]
        assert any(d["delta"] == "zen hi" for d in deltas)
        assert not any(e["type"] == "response.error" for e in events)
        assert b"[DONE]" in wfile.getvalue()

        # The go 401 is superseded: the only meter record is the zen 200.
        meter = _meter_events()
        assert len(meter) == 1
        assert meter[0]["provider"] == "zen"
        assert meter[0]["status"] == 200

    def test_go_401_invalid_key_no_fallback(self) -> None:
        _seed_collision()
        clear_api_key_cache()
        wfile = io.BytesIO()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "urllib.request.urlopen", side_effect=_go_http_error(401, INVALID_KEY_BODY)
        ) as urlopen:
            handle_streaming_request(responses_payload(stream=True), make_config(), "req", wfile)

        urlopen.assert_called_once()  # no second (zen) connect
        events = _sse_events(wfile)
        errors = [e for e in events if e["type"] == "response.error"]
        assert len(errors) == 1
        assert errors[0]["error"]["message"] == "upstream HTTP 401"
        assert b"[DONE]" in wfile.getvalue()
        meter = _meter_events()
        assert len(meter) == 1
        assert meter[0]["status"] == 401

    def test_go_200_with_data_no_fallback(self) -> None:
        _seed_collision()
        clear_api_key_cache()
        go_lines = [
            b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"go hi"}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n',
            b"data: [DONE]\n",
        ]
        wfile = io.BytesIO()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "urllib.request.urlopen", return_value=_UpstreamStream(go_lines)
        ) as urlopen:
            handle_streaming_request(responses_payload(stream=True), make_config(), "req", wfile)

        urlopen.assert_called_once()
        events = _sse_events(wfile)
        completed = [e for e in events if e["type"] == "response.completed"]
        assert len(completed) == 1
        assert completed[0]["response"]["output_text"] == "go hi"
        assert not any(e["type"] == "response.error" for e in events)

    def test_bare_slug_not_in_zen_ids_no_fallback(self) -> None:
        from opencode_go_proxy import zen_catalog as _zc

        _zc._ZEN_MODELS_CACHE = None
        with open(_zc.zen_models_path(), "w") as handle:
            json.dump({"fetched_at": "2026-08-14T00:00:00Z", "models": []}, handle)
        clear_api_key_cache()
        wfile = io.BytesIO()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "urllib.request.urlopen", side_effect=_go_http_error()
        ) as urlopen:
            handle_streaming_request(responses_payload(stream=True), make_config(), "req", wfile)

        urlopen.assert_called_once()
        events = _sse_events(wfile)
        errors = [e for e in events if e["type"] == "response.error"]
        assert len(errors) == 1
        assert errors[0]["error"]["message"] == "upstream HTTP 401"
        meter = _meter_events()
        assert meter and meter[0]["status"] == 401

    def test_zen_failure_mid_relay_meters_502(self) -> None:
        _seed_collision()
        clear_api_key_cache()
        wfile = io.BytesIO()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "urllib.request.urlopen",
            side_effect=[_go_http_error(), _AbortingStream()],
        ):
            handle_streaming_request(responses_payload(stream=True), make_config(), "req", wfile)

        events = _sse_events(wfile)
        errors = [e for e in events if e["type"] == "response.error"]
        assert len(errors) == 1
        assert "aborted" in errors[0]["error"]["message"]
        assert b"[DONE]" in wfile.getvalue()
        # The zen attempt's failure is what the meter records: 502 aborted,
        # never the superseded go 401 and never a 200 the client did not get.
        meter = _meter_events()
        assert len(meter) == 1
        assert meter[0]["provider"] == "zen"
        assert meter[0]["status"] == 502
        assert meter[0].get("streamAborted") is True

    def test_zen_connect_rejection_surfaces_zen_envelope(self) -> None:
        _seed_collision()
        clear_api_key_cache()
        zen_reject = json.dumps({"error": {"type": "ZenModelError", "message": "zen says no"}})
        wfile = io.BytesIO()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "urllib.request.urlopen",
            side_effect=[_go_http_error(), _go_http_error(400, zen_reject)],
        ):
            handle_streaming_request(responses_payload(stream=True), make_config(), "req", wfile)

        events = _sse_events(wfile)
        errors = [e for e in events if e["type"] == "response.error"]
        assert len(errors) == 1
        # The zen error envelope (type + message) is the terminal event, never
        # a second HTTP response inside the already-committed SSE stream.
        assert errors[0]["error"]["message"] == "zen says no"
        assert errors[0]["error"].get("code") == "ZenModelError"
        assert b"[DONE]" in wfile.getvalue()
        meter = _meter_events()
        assert len(meter) == 1
        assert meter[0]["provider"] == "zen"
        assert meter[0]["status"] == 400

    def test_stream_flag_and_request_id_preserved(self) -> None:
        _seed_collision()
        clear_api_key_cache()
        go_calls: list[tuple] = []
        zen_calls: list[tuple] = []

        def go_open(req, config, request_id, max_retries):
            go_calls.append((req, config, request_id, max_retries))
            raise _ConnectFailed(_go_http_error(), 0, GO_REJECT_BODY.encode())

        def zen_open(req, config, request_id, max_retries):
            zen_calls.append((req, config, request_id, max_retries))
            return _UpstreamStream(_zen_stream_lines()), 0

        wfile = io.BytesIO()
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.streaming._open_upstream_stream", side_effect=go_open
        ), mock.patch(
            "opencode_go_proxy.zen_upstream._open_upstream_stream", side_effect=zen_open
        ):
            handle_streaming_request(responses_payload(stream=True), make_config(), "req", wfile)

        assert len(go_calls) == 1
        assert len(zen_calls) == 1
        assert zen_calls[0][2] == "req"  # request_id preserved
        body = json.loads(zen_calls[0][0].data)
        assert body["stream"] is True  # stream flag preserved on the zen wire
        assert body["model"] == ZEN_SLUG  # original bare slug on the zen wire


class TestChatStreamFallback:
    """Chat-completions stream=true: the go-reject zen fallback mirrors the
    non-stream chat fallback (zen/ prefix payload handed to the zen handler).
    """

    def test_go_reject_falls_back_to_zen(self) -> None:
        _seed_collision()
        clear_api_key_cache()
        handler = _FakeHandler()
        payload = chat_payload()
        payload["stream"] = True
        config = make_config()

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.streaming._open_upstream_stream",
            side_effect=_ConnectFailed(_go_http_error(), 0, GO_REJECT_BODY.encode()),
        ), mock.patch("opencode_go_proxy.zen_upstream.handle_zen_chat_request") as zen:
            handle_chat_stream_passthrough(payload, config, "req", handler)

        zen.assert_called_once()
        called_handler, zen_payload, called_config, called_request_id = zen.call_args.args
        assert called_handler is handler
        assert called_config is config
        assert called_request_id == "req"
        assert zen_payload["model"] == f"zen/{ZEN_SLUG}"
        assert zen_payload["messages"] == payload["messages"]
        assert zen_payload["stream"] is True

    def test_go_401_invalid_key_relayed_verbatim_no_fallback(self) -> None:
        _seed_collision()
        clear_api_key_cache()
        handler = _FakeHandler()
        payload = chat_payload()
        payload["stream"] = True

        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "opencode_go_proxy.streaming._open_upstream_stream",
            side_effect=_ConnectFailed(_go_http_error(401, INVALID_KEY_BODY), 0, INVALID_KEY_BODY.encode()),
        ), mock.patch("opencode_go_proxy.zen_upstream.handle_zen_chat_request") as zen:
            handle_chat_stream_passthrough(payload, make_config(), "req", handler)

        zen.assert_not_called()
        assert handler.status == 401
        assert handler.wfile.getvalue() == INVALID_KEY_BODY.encode()
