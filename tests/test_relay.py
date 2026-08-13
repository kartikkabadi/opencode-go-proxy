"""Session-model inheritance for spawned threads (create_thread / send_message_to_thread)."""

import json
import unittest

from opencode_go_proxy.protocol import (
    inject_session_model,
    responses_payload_to_chat_payload,
)

SESSION_MODEL = "opencode-go/deepseek-v4-flash"


def thread_call(
    name: str = "create_thread",
    arguments: str = "{}",
    namespace: str | None = None,
) -> dict:
    item = {
        "type": "function_call",
        "call_id": "call_1",
        "name": name,
        "arguments": arguments,
    }
    if namespace:
        item["namespace"] = namespace
    return item


def payload_with(*items: dict) -> dict:
    return {
        "model": SESSION_MODEL,
        "input": list(items),
        "tools": [],
    }


class SessionModelInjectionTests(unittest.TestCase):
    def test_create_thread_without_model_injects_session_model(self) -> None:
        payload = payload_with(thread_call(arguments="{}"))
        result = inject_session_model(payload, SESSION_MODEL)
        item = result["input"][0]
        self.assertEqual(json.loads(item["arguments"]), {"model": SESSION_MODEL})

    def test_create_thread_with_explicit_model_unchanged(self) -> None:
        payload = payload_with(thread_call(arguments='{"model":"explicit-model"}'))
        result = inject_session_model(payload, SESSION_MODEL)
        item = result["input"][0]
        self.assertEqual(json.loads(item["arguments"]), {"model": "explicit-model"})

    def test_send_message_to_thread_without_model_injects(self) -> None:
        payload = payload_with(thread_call(name="send_message_to_thread", arguments="{}"))
        result = inject_session_model(payload, SESSION_MODEL)
        item = result["input"][0]
        self.assertEqual(json.loads(item["arguments"]), {"model": SESSION_MODEL})

    def test_flat_namespaced_name_preserved(self) -> None:
        payload = payload_with(thread_call(arguments="{}"))
        payload["input"][0]["name"] = "codex_app__create_thread"
        result = inject_session_model(payload, SESSION_MODEL)
        item = result["input"][0]
        self.assertEqual(item["name"], "codex_app__create_thread")
        self.assertEqual(json.loads(item["arguments"]), {"model": SESSION_MODEL})

    def test_native_namespace_name_form_handled(self) -> None:
        payload = payload_with(
            thread_call(namespace="codex_app", name="send_message_to_thread", arguments="{}")
        )
        result = inject_session_model(payload, SESSION_MODEL)
        item = result["input"][0]
        self.assertEqual(item["namespace"], "codex_app")
        self.assertEqual(item["name"], "send_message_to_thread")
        self.assertEqual(json.loads(item["arguments"]), {"model": SESSION_MODEL})

    def test_other_tools_untouched(self) -> None:
        payload = payload_with(
            {"type": "function_call", "call_id": "call_1", "name": "exec_command", "arguments": "{}"}
        )
        result = inject_session_model(payload, SESSION_MODEL)
        self.assertEqual(result, payload)

    def test_malformed_arguments_untouched(self) -> None:
        payload = payload_with(
            thread_call(arguments="not-json{"),
            thread_call(arguments='{"model":null,"x":1}'),
        )
        result = inject_session_model(payload, SESSION_MODEL)
        self.assertEqual(result, payload)
        self.assertEqual(result["input"][0]["arguments"], "not-json{")

    def test_end_to_end_chat_payload_model_in_tool_call_args(self) -> None:
        payload = payload_with(
            thread_call(arguments="{}"),
            {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
        )
        result = inject_session_model(payload, SESSION_MODEL)
        chat, _model, _stats = responses_payload_to_chat_payload(result)
        tool_call = chat["messages"][0]["tool_calls"][0]
        self.assertEqual(
            json.loads(tool_call["function"]["arguments"]),
            {"model": SESSION_MODEL},
        )


if __name__ == "__main__":
    unittest.main()
