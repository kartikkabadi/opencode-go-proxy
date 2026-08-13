"""codex_tools slice: capture, merge, spawn-model inheritance, web search tool, compact."""

import json
import os
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest import mock

import pytest

from opencode_go_proxy.app import ProxyConfig, ResponsesProxyHandler
from opencode_go_proxy.codex_tools import (
    capture_codex_app_tools,
    load_snapshot_tools,
    merge_codex_app_tools,
    state_tools_path,
)
from opencode_go_proxy.protocol import (
    chat_completion_to_response,
    inject_session_model,
    responses_payload_to_chat_payload,
    responses_tools_to_chat_tools,
)

SESSION_MODEL = "opencode-go/deepseek-v4-flash"

FAKE_PROMPT_INPUT = {
    "tools": [
        {
            "type": "namespace",
            "name": "codex_app",
            "tools": [
                {
                    "type": "function",
                    "name": "create_thread",
                    "description": "Create a separate task.",
                    "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
                },
                {
                    "type": "function",
                    "name": "list_threads",
                    "description": "List threads.",
                    "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
                },
            ],
        },
        {
            "type": "function",
            "name": "plugin_management__uninstall_plugin",
            "description": "Uninstall a plugin.",
            "parameters": {"type": "object", "properties": {"pluginId": {"type": "string"}}},
        },
    ]
}

FAKE_BIN = f"""#!/usr/bin/env python3
import json
import os
import sys

mode = os.environ.get("FAKE_PROMPT_INPUT_MODE", "tools")
if sys.argv[1] == "--version":
    print("codex-cli 0.test.0")
    raise SystemExit(0)
if mode == "messages":
    print(json.dumps([{{"type": "message", "role": "user", "content": []}}]))
elif mode == "invalid":
    print("not json")
else:
    print(json.dumps(json.loads(sys.stdin.read()) if os.environ.get("FAKE_PROMPT_FROM_STDIN") else {json.dumps(FAKE_PROMPT_INPUT)}))
"""


@pytest.fixture
def fake_codex_bin(tmp_path) -> str:
    path = tmp_path / "fake-codex"
    path.write_text(FAKE_BIN)
    path.chmod(0o755)
    return str(path)


def snapshot_names(tools=None) -> set[str]:
    tools = load_snapshot_tools() if tools is None else tools
    names: set[str] = set()
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else tool.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


def thread_call(name: str = "create_thread", arguments: str = "{}", namespace: str | None = None) -> dict:
    item = {"type": "function_call", "call_id": "call_1", "name": name, "arguments": arguments}
    if namespace:
        item["namespace"] = namespace
    return item


def payload_with(*items: dict) -> dict:
    return {"model": SESSION_MODEL, "input": list(items), "tools": []}


def make_config(port: int) -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=port,
        chat_base_url="https://mock-upstream.test/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=10,
        max_body_bytes=20 * 1024 * 1024,
    )


def mock_chat_response(content: str = "hello") -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "deepseek-v4-flash",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
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


class TestCapture:
    def test_capture_writes_state_snapshot(self, fake_codex_bin) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CODEX_BIN": fake_codex_bin}):
            tools = capture_codex_app_tools()
        assert tools is not None
        assert snapshot_names(tools) == {
            "codex_app__create_thread",
            "codex_app__list_threads",
            "plugin_management__uninstall_plugin",
        }
        with open(state_tools_path(), encoding="utf-8") as fh:
            snapshot = json.load(fh)
        assert snapshot["captured_with"] == "codex-cli 0.test.0"
        assert "captured_at" in snapshot

    def test_capture_writes_then_load_serves_snapshot(self, fake_codex_bin) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CODEX_BIN": fake_codex_bin}):
            capture_codex_app_tools()
        loaded = load_snapshot_tools()
        assert snapshot_names(loaded) == {
            "codex_app__create_thread",
            "codex_app__list_threads",
            "plugin_management__uninstall_plugin",
        }

    def test_capture_messages_only_returns_none(self, fake_codex_bin) -> None:
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_GO_PROXY_CODEX_BIN": fake_codex_bin, "FAKE_PROMPT_INPUT_MODE": "messages"},
        ):
            tools = capture_codex_app_tools()
        assert tools is None
        assert not os.path.exists(state_tools_path())

    def test_capture_invalid_output_returns_none(self, fake_codex_bin) -> None:
        with mock.patch.dict(
            os.environ,
            {"OPENCODE_GO_PROXY_CODEX_BIN": fake_codex_bin, "FAKE_PROMPT_INPUT_MODE": "invalid"},
        ):
            tools = capture_codex_app_tools()
        assert tools is None

    def test_capture_missing_binary_returns_none(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CODEX_BIN": ""}):
            tools = capture_codex_app_tools()
        assert tools is None


class TestSnapshotLoad:
    def test_fallback_loads_when_no_capture(self) -> None:
        tools = load_snapshot_tools()
        names = snapshot_names(tools)
        assert "codex_app__create_thread" in names
        assert "codex_app__wait_threads" in names
        assert "codex_app__automation_update" in names
        assert len(tools) >= 15

    def test_fallback_entries_have_chat_function_shape(self) -> None:
        for tool in load_snapshot_tools():
            function = tool["function"]
            assert tool["type"] == "function"
            assert function["name"].startswith(("codex_app__", "plugin_management__"))
            assert isinstance(function["description"], str)
            assert "parameters" in function


class TestMerge:
    def test_appends_full_snapshot_when_request_has_none(self) -> None:
        request_tools = [
            {"type": "function", "name": "exec_command", "description": "x", "parameters": {"type": "object"}}
        ]
        merged = merge_codex_app_tools(request_tools)
        assert snapshot_names(merged) == {"exec_command"} | snapshot_names()
        assert merged[0] is request_tools[0]

    def test_empty_list_gets_full_snapshot(self) -> None:
        merged = merge_codex_app_tools([])
        assert snapshot_names(merged) == snapshot_names()

    def test_skips_existing_flat_name_without_duplicating(self) -> None:
        mine = {
            "type": "function",
            "function": {"name": "codex_app__create_thread", "description": "client def", "parameters": {}},
        }
        merged = merge_codex_app_tools([mine])
        matches = [t for t in merged if t.get("function", {}).get("name") == "codex_app__create_thread"]
        assert len(matches) == 1
        assert matches[0] is mine

    def test_skips_existing_top_level_name(self) -> None:
        mine = {"type": "function", "name": "codex_app__wait_threads", "description": "client def"}
        merged = merge_codex_app_tools([mine])
        matches = [t for t in merged if t.get("name") == "codex_app__wait_threads"]
        assert len(matches) == 1
        assert matches[0] is mine

    def test_namespace_group_kept_and_missing_appended(self) -> None:
        group = {"type": "namespace", "name": "codex_app", "tools": [{"type": "function", "name": "create_thread"}]}
        merged = merge_codex_app_tools([group])
        assert merged[0] is group
        flat = {t["function"]["name"] for t in merged if t.get("type") == "function"}
        assert flat == snapshot_names() - {"codex_app__create_thread"}

    def test_no_op_when_all_present(self) -> None:
        request_tools = [
            {"type": "function", "function": {"name": name, "description": "d", "parameters": {}}}
            for name in sorted(snapshot_names())
        ]
        merged = merge_codex_app_tools(request_tools)
        assert len(merged) == len(request_tools)
        assert [t["function"]["name"] for t in merged] == [t["function"]["name"] for t in request_tools]

    def test_non_list_unchanged(self) -> None:
        assert merge_codex_app_tools(None) is None
        assert merge_codex_app_tools("x") == "x"

    def test_chat_tools_merge_appends_snapshot(self) -> None:
        chat_tools, stats = responses_tools_to_chat_tools(
            [{"type": "function", "name": "exec_command", "description": "x", "parameters": {"type": "object"}}]
        )
        names = {t["function"]["name"] for t in chat_tools}
        assert "exec_command" in names
        assert "codex_app__create_thread" in names
        assert stats["dropped_tools"] == 0


class TestSpawnModelInheritance:
    def test_create_thread_without_model_injects_session_model(self) -> None:
        result = inject_session_model(payload_with(thread_call(arguments="{}")), SESSION_MODEL)
        assert json.loads(result["input"][0]["arguments"]) == {"model": SESSION_MODEL}

    def test_create_thread_explicit_model_skipped(self) -> None:
        payload = payload_with(thread_call(arguments='{"model":"gpt-5.6-luna"}'))
        result = inject_session_model(payload, SESSION_MODEL)
        assert json.loads(result["input"][0]["arguments"]) == {"model": "gpt-5.6-luna"}

    def test_chatgpt_work_cloud_target_skipped(self) -> None:
        payload = payload_with(thread_call(arguments='{"target": {"type": "chatgptWorkCloud"}}'))
        result = inject_session_model(payload, SESSION_MODEL)
        assert result["input"][0]["arguments"] == '{"target": {"type": "chatgptWorkCloud"}}'

    def test_flat_codex_app_name_injects(self) -> None:
        payload = payload_with(thread_call(name="codex_app__create_thread", arguments="{}"))
        result = inject_session_model(payload, SESSION_MODEL)
        assert json.loads(result["input"][0]["arguments"]) == {"model": SESSION_MODEL}

    def test_send_message_to_thread_keeps_its_settings(self) -> None:
        payload = payload_with(thread_call(name="send_message_to_thread", arguments="{}"))
        result = inject_session_model(payload, SESSION_MODEL)
        assert result["input"][0]["arguments"] == "{}"


class TestWebSearchPreview:
    def test_bare_type_translated_not_dropped(self) -> None:
        chat_tools, stats = responses_tools_to_chat_tools([{"type": "web_search_preview"}])
        assert stats["dropped_tools"] == 0
        assert chat_tools[0]["function"]["name"] == "web_search_preview"
        assert chat_tools[0]["function"]["parameters"]["required"] == ["query"]

    def test_mixed_with_namespace_tools_keeps_everything(self) -> None:
        tools = [
            {"type": "namespace", "name": "codex_app", "tools": [{"type": "function", "name": "create_thread"}]},
            {"type": "web_search_preview"},
        ]
        chat_tools, stats = responses_tools_to_chat_tools(tools)
        names = {t["function"]["name"] for t in chat_tools}
        assert "codex_app__create_thread" in names
        assert "web_search_preview" in names
        assert stats["dropped_tools"] == 0


class TestCompact:
    def test_compact_payload_round_trips_through_normal_translation(self) -> None:
        payload = {
            "model": "deepseek-v4-flash",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Previous conversation summary: user asked about pricing.",
                        }
                    ],
                },
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
            ],
        }
        chat, _model, stats = responses_payload_to_chat_payload(payload)
        assert stats["messages"]["input_items"] == 2
        assert "Previous conversation summary" in chat["messages"][0]["content"]
        assert "tools" not in chat

        response = chat_completion_to_response(mock_chat_response("summary answer"), request_model="deepseek-v4-flash")
        assert response["status"] == "completed"
        assert response["object"] == "response"
        assert response["output"][0]["type"] == "message"
        assert response["output_text"] == "summary answer"

    def test_compact_query_path_routes_to_normal_translation(self, server) -> None:
        port, _ = server
        mock_resp = mock_chat_response("compact answer")
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "urllib.request.urlopen", return_value=MockUpstreamResponse(json.dumps(mock_resp).encode())
        ):
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request(
                "POST",
                "/v1/responses?compact=true",
                json.dumps({"model": "deepseek-v4-flash", "input": "continue"}),
                {"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            body = json.loads(resp.read())
            conn.close()
        assert resp.status == 200
        assert body["status"] == "completed"
        assert body["output_text"] == "compact answer"
