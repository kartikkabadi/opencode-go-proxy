import gzip
import os
import subprocess
import unittest
from http import HTTPStatus
from unittest import mock

import zstandard

from opencode_go_proxy.app import (
    ProxyConfig,
    ProxyError,
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


if __name__ == "__main__":
    unittest.main()
