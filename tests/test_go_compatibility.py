import io
import json
from email.message import Message
from unittest import mock

import pytest

from opencode_go_proxy import catalog, go_upstream, zen_upstream
from opencode_go_proxy.config import ProxyConfig
from opencode_go_proxy.errors import ProxyError
from opencode_go_proxy.go_models import (
    ANTHROPIC_MESSAGES,
    DOCUMENTED_CHAT_MODELS,
    DOCUMENTED_GO_MODELS,
    DOCUMENTED_MESSAGES_MODELS,
    DOCUMENTED_RESPONSES_MODELS,
    OPENAI_CHAT,
    OPENAI_RESPONSES,
    go_family_for,
)
from opencode_go_proxy.opencode_session import resolve_opencode_session
from opencode_go_proxy.protocol import restore_namespaced_function_calls


def config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=8787,
        chat_base_url="https://opencode.ai/zen/go/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=10,
        max_body_bytes=1024 * 1024,
    )


class Handler:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value
        self.wfile = io.BytesIO()
        self.status: int | None = None
        self.response_headers: list[tuple[str, str]] = []

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.response_headers.append((name, value))

    def end_headers(self) -> None:
        pass


def request_body(request) -> dict:
    return json.loads(request.data)


def request_headers(request) -> dict[str, str]:
    return {name.lower(): value for name, value in request.header_items()}


def response(value: dict) -> mock.Mock:
    raw = json.dumps(value).encode()
    return mock.Mock(
        status=200,
        headers={},
        read=lambda: raw,
        __enter__=lambda current: current,
        __exit__=lambda *args: False,
    )


def test_documented_catalog_has_all_30_models_in_their_official_families() -> None:
    assert len(DOCUMENTED_GO_MODELS) == 30
    assert len(DOCUMENTED_RESPONSES_MODELS) == 4
    assert len(DOCUMENTED_CHAT_MODELS) == 18
    assert len(DOCUMENTED_MESSAGES_MODELS) == 8
    assert {go_family_for(model) for model in DOCUMENTED_RESPONSES_MODELS} == {
        OPENAI_RESPONSES
    }
    assert {go_family_for(model) for model in DOCUMENTED_CHAT_MODELS} == {
        OPENAI_CHAT
    }
    assert {go_family_for(model) for model in DOCUMENTED_MESSAGES_MODELS} == {
        ANTHROPIC_MESSAGES
    }
    seed = catalog.load_seed_compact()
    assert seed is not None
    newly_certified = {"longcat-2.5-preview-free", "mimo-v2.6-flash"}
    assert newly_certified <= DOCUMENTED_CHAT_MODELS
    assert go_family_for("longcat-2.5-preview-free") == OPENAI_CHAT
    assert go_family_for("mimo-v2.6-flash") == OPENAI_CHAT
    pre_existing = DOCUMENTED_GO_MODELS - newly_certified
    assert pre_existing <= {str(model["slug"]) for model in seed["models"]}


def test_explicit_and_safe_bare_selection_reject_unknown_ids() -> None:
    assert go_upstream.go_request_identity("opencode-go/grok-4.6") == (
        "grok-4.6",
        OPENAI_RESPONSES,
    )
    assert go_upstream.go_request_identity("glm-5.3") == (
        "glm-5.3",
        OPENAI_CHAT,
    )
    with pytest.raises(ProxyError) as unknown:
        go_upstream.go_request_identity("opencode-go/not-a-go-model")
    assert unknown.value.status == 400
    with pytest.raises(ProxyError):
        go_upstream.go_request_identity("opencode-go/")


@pytest.mark.parametrize("model", sorted(DOCUMENTED_GO_MODELS))
@pytest.mark.parametrize("stream", [False, True])
def test_responses_adapter_supports_every_documented_model(
    model: str,
    stream: bool,
) -> None:
    selected_model = f"opencode-go/{model}"
    bare_id, family = go_upstream.go_request_identity(selected_model)
    url, body, headers = zen_upstream._build_family_request(
        {"model": selected_model, "input": "hello", "stream": stream},
        family,
        bare_id,
        "go-key",
        stream=stream,
        session_model=selected_model,
        base_url=config().chat_base_url,
        extra_headers={"x-opencode-session": "session-1"},
        function_tools_only=True,
    )
    path = {
        OPENAI_RESPONSES: "/responses",
        OPENAI_CHAT: "/chat/completions",
        ANTHROPIC_MESSAGES: "/messages",
    }[family]
    auth = "x-api-key" if family == ANTHROPIC_MESSAGES else "authorization"
    assert url.endswith(path)
    assert body["model"] == bare_id
    assert body["stream"] is stream
    assert headers[auth] == (
        "Bearer go-key" if auth == "authorization" else "go-key"
    )
    assert headers["x-opencode-session"] == "session-1"
    assert headers["user-agent"].startswith("opencode-go-proxy/")
    assert ("authorization" in headers) is (auth == "authorization")
    assert ("x-api-key" in headers) is (auth == "x-api-key")


def test_responses_models_receive_only_ordinary_function_tools() -> None:
    payload = {
        "model": "opencode-go/muse-spark-1.3-contributor",
        "input": "edit",
        "tools": [
            {
                "type": "namespace",
                "name": "repo",
                "tools": [
                    {
                        "type": "function",
                        "name": "read",
                        "description": "Read a file",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                    }
                ],
            },
            {
                "type": "custom",
                "name": "apply_patch",
                "description": "Apply a patch",
            },
        ],
        "tool_choice": {
            "type": "function",
            "namespace": "repo",
            "name": "read",
        },
    }
    url, body, _headers = zen_upstream._build_family_request(
        payload,
        OPENAI_RESPONSES,
        "muse-spark-1.3-contributor",
        "go-key",
        stream=False,
        session_model=payload["model"],
        base_url=config().chat_base_url,
        function_tools_only=True,
    )
    assert url.endswith("/responses")
    tool_names = [tool["name"] for tool in body["tools"]]
    assert "repo__read" in tool_names
    assert "apply_patch" in tool_names
    assert {tool["type"] for tool in body["tools"]} == {"function"}
    assert body["tool_choice"] == {"type": "function", "name": "repo__read"}


def test_namespaced_calls_are_restored_for_nonstream_and_stream_events() -> None:
    value = {
        "output": [
            {
                "type": "function_call",
                "name": "repo__read",
                "arguments": "{}",
            }
        ]
    }
    restored = restore_namespaced_function_calls(value)
    assert restored["output"][0]["namespace"] == "repo"
    assert restored["output"][0]["name"] == "read"
    line = b'data: {"type":"response.output_item.done","item":{"type":"function_call","name":"repo__read"}}\n'
    transformed = zen_upstream._restore_responses_sse_line(line)
    event = json.loads(transformed.removeprefix(b"data: "))
    assert event["item"]["namespace"] == "repo"
    assert event["item"]["name"] == "read"


def test_session_identity_forwards_aliases_and_derives_stably() -> None:
    payload = {"input": [{"role": "user", "content": "same conversation"}]}
    assert (
        resolve_opencode_session({"x-opencode-session": "official"}, payload)
        == "official"
    )
    assert resolve_opencode_session({"thread-id": "thread-7"}, payload) == "thread-7"
    derived = resolve_opencode_session({}, payload)
    assert derived == resolve_opencode_session({}, payload)
    assert derived != resolve_opencode_session(
        {},
        {"input": [{"role": "user", "content": "different conversation"}]},
    )


def test_go_chat_and_messages_verbatim_paths_keep_credentials_separate() -> None:
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(request)
        if request.full_url.endswith("/messages"):
            return response({"id": "msg_1", "content": []})
        return response({"id": "chatcmpl_1", "choices": []})

    with (
        mock.patch(
            "opencode_go_proxy.go_upstream.resolve_api_key",
            return_value="go-key",
        ),
        mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        chat_handler = Handler({"x-opencode-session": "chat-session"})
        go_upstream.handle_go_chat_request(
            chat_handler,
            {
                "model": "opencode-go/glm-5.3",
                "messages": [{"role": "user", "content": "hi"}],
            },
            config(),
            "chat-request",
        )
        messages_handler = Handler({"x-opencode-session": "messages-session"})
        go_upstream.handle_go_messages_request(
            messages_handler,
            {
                "model": "opencode-go/minimax-m3",
                "messages": [{"role": "user", "content": "hi"}],
            },
            config(),
            "messages-request",
        )

    chat_request, messages_request = captured
    assert chat_request.full_url.endswith("/chat/completions")
    assert request_body(chat_request)["model"] == "glm-5.3"
    assert request_headers(chat_request)["authorization"] == "Bearer go-key"
    assert request_headers(chat_request)["x-opencode-session"] == "chat-session"
    assert "x-api-key" not in request_headers(chat_request)
    assert messages_request.full_url.endswith("/messages")
    assert request_body(messages_request)["model"] == "minimax-m3"
    assert request_headers(messages_request)["x-api-key"] == "go-key"
    assert request_headers(messages_request)["x-opencode-session"] == (
        "messages-session"
    )
    assert "authorization" not in request_headers(messages_request)


@pytest.mark.parametrize("model", sorted(DOCUMENTED_CHAT_MODELS))
def test_direct_chat_surface_supports_every_chat_model(model: str) -> None:
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(request)
        return response({"id": "chatcmpl_1", "choices": []})

    with (
        mock.patch(
            "opencode_go_proxy.go_upstream.resolve_api_key",
            return_value="go-key",
        ),
        mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        handler = Handler()
        go_upstream.handle_go_chat_request(
            handler,
            {
                "model": f"opencode-go/{model}",
                "messages": [{"role": "user", "content": "hi"}],
            },
            config(),
            "chat-request",
        )

    assert handler.status == 200
    assert captured[0].full_url.endswith("/chat/completions")
    assert request_body(captured[0])["model"] == model


@pytest.mark.parametrize("model", sorted(DOCUMENTED_MESSAGES_MODELS))
def test_direct_messages_surface_supports_every_messages_model(model: str) -> None:
    captured = []

    def fake_urlopen(request, **kwargs):
        captured.append(request)
        return response({"id": "msg_1", "content": []})

    with (
        mock.patch(
            "opencode_go_proxy.go_upstream.resolve_api_key",
            return_value="go-key",
        ),
        mock.patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        handler = Handler()
        go_upstream.handle_go_messages_request(
            handler,
            {
                "model": f"opencode-go/{model}",
                "messages": [{"role": "user", "content": "hi"}],
            },
            config(),
            "messages-request",
        )

    assert handler.status == 200
    assert captured[0].full_url.endswith("/messages")
    assert request_body(captured[0])["model"] == model


@pytest.mark.parametrize(
    "model",
    sorted(DOCUMENTED_RESPONSES_MODELS | DOCUMENTED_MESSAGES_MODELS),
)
def test_direct_chat_rejects_non_chat_models(model: str) -> None:
    with pytest.raises(ProxyError) as error:
        go_upstream.handle_go_chat_request(
            Handler(),
            {"model": f"opencode-go/{model}", "messages": []},
            config(),
            "chat-request",
        )
    assert error.value.status == 400
    assert error.value.error_type == "invalid_request_error"


@pytest.mark.parametrize(
    "model",
    sorted(DOCUMENTED_RESPONSES_MODELS | DOCUMENTED_CHAT_MODELS),
)
def test_direct_messages_rejects_non_messages_models(model: str) -> None:
    with pytest.raises(ProxyError) as error:
        go_upstream.handle_go_messages_request(
            Handler(),
            {"model": f"opencode-go/{model}", "messages": []},
            config(),
            "messages-request",
        )
    assert error.value.status == 400
    assert error.value.error_type == "invalid_request_error"


def test_merged_catalog_uses_explicit_go_slugs_for_native_collisions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("OPENCODE_GO_PROXY_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        "opencode_go_proxy.native_models.load_native_capture",
        lambda: {
            "models": [
                {
                    "slug": "gpt-5.6-luna",
                    "display_name": "GPT-5.6 Luna",
                }
            ]
        },
    )
    merged = catalog.render_merged_catalog()
    slugs = {model["slug"] for model in merged["models"]}
    assert "gpt-5.6-luna" in slugs
    assert "opencode-go/gpt-5.6-luna" in slugs
    assert "opencode-go/muse-spark-1.3-contributor" in slugs
