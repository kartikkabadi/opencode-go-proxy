import gzip
import io
import json
import os
import subprocess
import unittest
from http import HTTPStatus
from unittest import mock

import zstandard

from opencode_go_proxy.app import (
    ProxyConfig,
    ProxyError,
    _caption_timeout_sec,
    _mask_trace_body,
    decode_request_body,
    resolve_api_key,
)


def make_config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=8787,
        chat_base_url="https://opencode.ai/zen/go/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=1,
        max_body_bytes=20 * 1024 * 1024,
    )


class CredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reset the module-level cache between tests.
        import opencode_go_proxy.app as app_mod
        app_mod._api_key_cache = None

    def test_env_key_wins_without_keychain_lookup(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "env-key"}, clear=True), mock.patch("opencode_go_proxy.app.subprocess.run") as run:
            self.assertEqual(resolve_api_key(make_config(), "req"), "env-key")

        run.assert_not_called()

    def test_keychain_lookup_uses_first_line(self) -> None:
        completed = subprocess.CompletedProcess(
            ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", "opencode-go-api-key", "-w"],
            0,
            stdout="keychain-key\n",
            stderr="",
        )
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("opencode_go_proxy.app.subprocess.run", return_value=completed):
            self.assertEqual(resolve_api_key(make_config(), "req"), "keychain-key")

    def test_missing_key_names_env_and_keychain(self) -> None:
        completed = subprocess.CompletedProcess(
            ["security", "find-generic-password", "-a", os.environ.get("USER", ""), "-s", "opencode-go-api-key", "-w"],
            1,
            stdout="",
            stderr="could not be found",
        )
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "opencode_go_proxy.app.subprocess.run", return_value=completed
        ), self.assertRaises(ProxyError) as ctx:
            resolve_api_key(make_config(), "req")

        self.assertEqual(ctx.exception.status, HTTPStatus.UNAUTHORIZED)
        self.assertIn("$OPENCODE_GO_API_KEY", ctx.exception.message)
        self.assertIn("keychain", ctx.exception.message)

    def test_env_falls_back_to_standard_opencode_key(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_API_KEY": "std-key"}, clear=True), mock.patch(
            "opencode_go_proxy.app.subprocess.run"
        ) as run:
            self.assertEqual(resolve_api_key(make_config(), "req"), "std-key")

        run.assert_not_called()

    def test_keychain_falls_back_to_codex_router_service(self) -> None:
        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            service = args[args.index("-s") + 1]
            if service == "codex-router-opencode-go":
                return subprocess.CompletedProcess(args, 0, stdout="router-key\n", stderr="")
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="not found")

        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "opencode_go_proxy.app.subprocess.run", side_effect=fake_run
        ):
            self.assertEqual(resolve_api_key(make_config(), "req"), "router-key")


class RequestBodyDecodeTests(unittest.TestCase):
    def test_identity_body_passes_through(self) -> None:
        body = b'{"a": 1}'
        self.assertEqual(decode_request_body(body, "", 10 * 1024 * 1024), body)
        self.assertEqual(decode_request_body(body, "identity", 10 * 1024 * 1024), body)

    def test_zstd_body_is_decompressed(self) -> None:
        raw = b'{"model": "deepseek-v4-flash", "input": "hi"}'
        compressed = zstandard.ZstdCompressor().compress(raw)
        self.assertEqual(decode_request_body(compressed, "zstd", 10 * 1024 * 1024), raw)

    def test_gzip_body_is_decompressed(self) -> None:
        raw = b'{"model": "deepseek-v4-flash"}'
        self.assertEqual(decode_request_body(gzip.compress(raw), "gzip", 10 * 1024 * 1024), raw)

    def test_bad_zstd_body_raises_400(self) -> None:
        with self.assertRaises(ProxyError) as ctx:
            decode_request_body(b"not-zstd-data", "zstd", 10 * 1024 * 1024)
        self.assertEqual(ctx.exception.status, HTTPStatus.BAD_REQUEST)

    def test_unsupported_encoding_raises_400(self) -> None:
        with self.assertRaises(ProxyError) as ctx:
            decode_request_body(b"{}", "br", 10 * 1024 * 1024)
        self.assertEqual(ctx.exception.status, HTTPStatus.BAD_REQUEST)

    def test_decompression_is_bounded(self) -> None:
        # A tiny compressed body that would decompress past the cap must fail
        # instead of exhausting memory.
        raw = b"x" * (8 * 1024 * 1024)
        compressed = zstandard.ZstdCompressor().compress(raw)
        with self.assertRaises(ProxyError) as ctx:
            decode_request_body(compressed, "zstd", 1024)
        self.assertEqual(ctx.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

    def test_gzip_decompression_is_bounded(self) -> None:
        # A small gzip body that decompresses past the cap must 413 instead of
        # ballooning into a huge allocation (gzip.decompress has no output cap).
        raw = b"x" * (8 * 1024 * 1024)
        compressed = gzip.compress(raw)
        with self.assertRaises(ProxyError) as ctx:
            decode_request_body(compressed, "gzip", 1024)
        self.assertEqual(ctx.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)


class _UpstreamStream:
    """Minimal stand-in for an upstream SSE response: iterable + context manager."""

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


def _run_stream(lines: list[bytes]) -> list[dict]:
    """Drive handle_streaming_request against `lines` and parse its SSE events."""
    import opencode_go_proxy.app as app_mod
    from opencode_go_proxy.app import handle_streaming_request

    app_mod._api_key_cache = None
    cfg = make_config()
    payload = {"model": "deepseek-v4-flash", "input": "hi", "stream": True}
    wfile = io.BytesIO()
    with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), \
         mock.patch("urllib.request.urlopen", return_value=_UpstreamStream(lines)):
        handle_streaming_request(payload, cfg, "req-stream", wfile)
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


class StreamingSseTests(unittest.TestCase):
    def test_reasoning_then_split_tool_call_indices_names_ids(self) -> None:
        # Reasoning followed by a tool call whose name arrives in chunks
        # ('read_' + 'file'). The function_call must stream at output_index 1
        # (not collide with the closed reasoning item at 0), carry the full
        # name on added and done, and keep the same id in response.completed.
        lines = [
            b'data: {"id":"1","choices":[{"index":0,"delta":{"reasoning_content":"Let me think"}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"read_"}}]}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"name":"file"}}]}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\":\\"README.md\\"}"}}]}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n',
            b'data: [DONE]\n',
        ]
        events = _run_stream(lines)
        added = [e for e in events if e["type"] == "response.output_item.added"]
        done = [e for e in events if e["type"] == "response.output_item.done"]
        reasoning_added = next(e for e in added if e["item"]["type"] == "reasoning")
        fc_added = next(e for e in added if e["item"]["type"] == "function_call")
        fc_done = next(e for e in done if e["item"]["type"] == "function_call")
        # Reasoning keeps output_index 0; the tool call takes 1, never 0.
        self.assertEqual(reasoning_added["output_index"], 0)
        self.assertEqual(fc_added["output_index"], 1)
        # The split tool name must be complete on both added and done.
        self.assertEqual(fc_added["item"]["name"], "read_file")
        self.assertEqual(fc_done["item"]["name"], "read_file")
        # response.completed reuses the streamed item ids (no ghost items).
        completed = next(e for e in events if e["type"] == "response.completed")["response"]
        fc_out = next(o for o in completed["output"] if o["type"] == "function_call")
        reasoning_out = next(o for o in completed["output"] if o["type"] == "reasoning")
        self.assertEqual(fc_out["id"], fc_added["item"]["id"])
        self.assertEqual(reasoning_out["id"], reasoning_added["item"]["id"])

    def test_reasoning_then_text_streams_at_index_1(self) -> None:
        # Text that follows a reasoning item must stream at output_index 1,
        # not 0 (same collision class as the tool-call path).
        lines = [
            b'data: {"id":"1","choices":[{"index":0,"delta":{"reasoning_content":"think"}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{"content":"answer"}}]}\n',
            b'data: {"id":"1","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n',
            b'data: [DONE]\n',
        ]
        events = _run_stream(lines)
        added = [e for e in events if e["type"] == "response.output_item.added"]
        msg_added = next(e for e in added if e["item"]["type"] == "message")
        self.assertEqual(msg_added["output_index"], 1)
        deltas = [e for e in events if e["type"] == "response.output_text.delta"]
        self.assertTrue(deltas)
        self.assertEqual(deltas[0]["output_index"], 1)
        self.assertEqual(deltas[0]["item_id"], msg_added["item"]["id"])
        completed = next(e for e in events if e["type"] == "response.completed")["response"]
        msg_out = next(o for o in completed["output"] if o["type"] == "message")
        self.assertEqual(msg_out["id"], msg_added["item"]["id"])


class MaskTraceBodyTests(unittest.TestCase):
    def test_masks_sk_tokens_and_authorization_header(self) -> None:
        masked = _mask_trace_body(
            '{"error":"invalid","Authorization": "Bearer sk-abc123DEF456ghi789jkl"} sk-abc123DEF456ghi789jkl'
        )
        self.assertNotIn("sk-abc123DEF456ghi789jkl", masked)
        self.assertNotIn("Bearer", masked)

    def test_masks_block_form_authorization_header(self) -> None:
        masked = _mask_trace_body("Authorization: Bearer sk-abc123DEF456ghi789jkl")
        self.assertNotIn("sk-abc123DEF456ghi789jkl", masked)
        self.assertNotIn("Bearer", masked)

    def test_leaves_plain_body_untouched(self) -> None:
        body = "upstream is having a bad day"
        self.assertEqual(_mask_trace_body(body), body)

    def test_truncates_to_limit(self) -> None:
        masked = _mask_trace_body("x" * 5000, limit=100)
        self.assertEqual(len(masked), 100)


class CaptionTimeoutTests(unittest.TestCase):
    def test_defaults_to_30_seconds(self) -> None:
        self.assertEqual(_caption_timeout_sec(), 30.0)

    def test_env_override_applies(self) -> None:
        with unittest.mock.patch.dict("os.environ", {"OPENCODE_GO_PROXY_CAPTION_TIMEOUT_SEC": "90"}):
            self.assertEqual(_caption_timeout_sec(), 90.0)

    def test_malformed_env_falls_back_to_default(self) -> None:
        with unittest.mock.patch.dict("os.environ", {"OPENCODE_GO_PROXY_CAPTION_TIMEOUT_SEC": "not-a-number"}):
            self.assertEqual(_caption_timeout_sec(), 30.0)

    def test_zero_env_is_clamped_to_one_second(self) -> None:
        with unittest.mock.patch.dict("os.environ", {"OPENCODE_GO_PROXY_CAPTION_TIMEOUT_SEC": "0"}):
            self.assertEqual(_caption_timeout_sec(), 1.0)


if __name__ == "__main__":
    unittest.main()
