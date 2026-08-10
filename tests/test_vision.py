"""Unit tests for the vision caption helpers (plan 001 latency subset)."""

import base64
import io
import os
import shutil
import struct
import time
import urllib.error
import urllib.request
import zlib
from unittest import mock

import pytest

from opencode_go_proxy import vision
from opencode_go_proxy.app import ProxyConfig
from opencode_go_proxy.errors import ProxyError
from opencode_go_proxy.protocol import IMAGE_MODEL_DEFAULT
from opencode_go_proxy.upstream import call_upstream_chat
from opencode_go_proxy.upstream import caption_timeout_sec as _caption_timeout_sec


def make_config() -> ProxyConfig:
    return ProxyConfig(
        bind="127.0.0.1",
        port=8787,
        chat_base_url="https://opencode.ai/zen/go/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=10,
        max_body_bytes=20 * 1024 * 1024,
    )


def make_png(width: int = 64, height: int = 64) -> bytes:
    """Small solid-red PNG, good enough for sips to read."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + b"\xff\x00\x00" * width
    idat = zlib.compress(row * height)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


class TestCaptionCache:
    def test_miss_then_hit(self) -> None:
        cache = vision.CaptionCache()
        data = b"abc"
        assert cache.get(data) is None
        cache.put(data, "caption-one")
        assert cache.get(data) == "caption-one"

    def test_keyed_by_decoded_bytes_not_url_text(self) -> None:
        cache = vision.CaptionCache()
        png = make_png()
        cache.put(vision.image_bytes_for_cache(data_url(png)), "same")
        assert cache.get(vision.image_bytes_for_cache(data_url(png))) == "same"

    def test_expiry(self) -> None:
        cache = vision.CaptionCache(ttl_sec=0.05)
        cache.put(b"x", "soon-gone")
        assert cache.get(b"x") == "soon-gone"
        time.sleep(0.08)
        assert cache.get(b"x") is None

    def test_evicts_oldest_when_full(self) -> None:
        cache = vision.CaptionCache(max_entries=2)
        cache.put(b"1", "one")
        time.sleep(0.01)
        cache.put(b"2", "two")
        time.sleep(0.01)
        cache.put(b"3", "three")
        assert len(cache) == 2
        assert cache.get(b"1") is None
        assert cache.get(b"2") == "two"
        assert cache.get(b"3") == "three"

    def test_clear(self) -> None:
        cache = vision.CaptionCache()
        cache.put(b"x", "c")
        cache.clear()
        assert len(cache) == 0


class TestCaptionModelSelection:
    def test_defaults_to_turn_model(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert vision.resolve_caption_model("deepseek-v4-flash") == "deepseek-v4-flash"

    def test_codex_image_model_override_wins(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_IMAGE_MODEL": "mimo-v2.5"}, clear=True):
            assert vision.resolve_caption_model("deepseek-v4-flash") == "mimo-v2.5"

    def test_caption_model_auto_uses_turn_model(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_MODEL": "auto"}, clear=True):
            assert vision.resolve_caption_model("deepseek-v4-flash") == "deepseek-v4-flash"

    def test_caption_model_explicit_pins_engine(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_MODEL": "deepseek-v4-pro"}, clear=True):
            assert vision.resolve_caption_model("deepseek-v4-flash") == "deepseek-v4-pro"

    def test_codex_image_model_beats_caption_model(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CODEX_IMAGE_MODEL": "mimo-v2.5", "OPENCODE_GO_PROXY_CAPTION_MODEL": "deepseek-v4-pro"},
            clear=True,
        ):
            assert vision.resolve_caption_model("deepseek-v4-flash") == "mimo-v2.5"

    def test_empty_target_falls_back_to_mimo(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert vision.resolve_caption_model("") == IMAGE_MODEL_DEFAULT


class TestCaptionDetail:
    def test_defaults_to_low(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert vision.caption_detail() == "low"

    def test_env_high(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_DETAIL": "high"}, clear=True):
            assert vision.caption_detail() == "high"

    def test_env_none_disables(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_DETAIL": "none"}, clear=True):
            assert vision.caption_detail() is None

    def test_unknown_value_falls_back_to_low(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_DETAIL": "weird"}, clear=True):
            assert vision.caption_detail() == "low"


class TestCaptionPayload:
    def test_includes_detail_low(self) -> None:
        url = "data:image/png;base64,AAAA"
        payload = vision.build_caption_payload(url, "deepseek-v4-flash", detail="low")
        image_url = payload["messages"][0]["content"][1]["image_url"]
        assert image_url == {"url": url, "detail": "low"}
        assert payload["model"] == "deepseek-v4-flash"
        assert payload["max_tokens"] == 200
        assert payload["stream"] is False

    def test_detail_none_omits_key(self) -> None:
        url = "https://example.com/x.png"
        payload = vision.build_caption_payload(url, "mimo-v2.5", detail=None)
        assert payload["messages"][0]["content"][1]["image_url"] == {"url": url}


class TestImageBytes:
    def test_data_url_decodes_to_payload(self) -> None:
        png = make_png()
        assert vision.image_bytes_for_cache(data_url(png)) == png

    def test_remote_url_uses_url_string(self) -> None:
        url = "https://example.com/shot.png"
        assert vision.image_bytes_for_cache(url) == url.encode("utf-8")

    def test_invalid_data_url_falls_back_to_string(self) -> None:
        url = "data:image/png;base64,!!!"
        assert vision.image_bytes_for_cache(url) == url.encode("utf-8")


@pytest.mark.skipif(shutil.which("sips") is None, reason="sips is macOS-only")
class TestDownscale:
    def test_downscales_data_url_to_jpeg(self) -> None:
        result = vision.downscale_data_url(data_url(make_png(320, 200)))
        assert result is not None
        assert result.startswith("data:image/jpeg;base64,")
        decoded = base64.b64decode(result.split(",", 1)[1])
        assert len(decoded) > 0

    def test_rejects_non_data_url(self) -> None:
        assert vision.downscale_data_url("https://example.com/x.png") is None

    def test_rejects_garbage_png(self) -> None:
        url = "data:image/png;base64," + base64.b64encode(b"not-a-png").decode("ascii")
        assert vision.downscale_data_url(url) is None


class TestCaptionBudget:
    def test_default_timeout_is_30_seconds(self) -> None:
        from opencode_go_proxy import upstream

        assert upstream.DEFAULT_CAPTION_TIMEOUT_SEC == 30.0

    def test_env_override_applies(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_TIMEOUT_SEC": "45"}, clear=True):
            assert _caption_timeout_sec() == 45.0

    def test_call_upstream_chat_max_retries_zero(self) -> None:
        from opencode_go_proxy.secrets import clear_api_key_cache

        clear_api_key_cache()
        err = urllib.error.HTTPError(
            "https://mock.test/v1/chat/completions", 503, "Service Unavailable",
            {}, io.BytesIO(b'{"error":"down"}'),
        )
        payload = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}], "stream": False}
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}, clear=True), mock.patch(
            "urllib.request.urlopen", side_effect=err
        ) as urlopen, pytest.raises(ProxyError) as ctx:
            call_upstream_chat(payload, make_config(), "req", max_retries=0)
        assert ctx.value.status == 503
        assert urlopen.call_count == 1

    def test_call_upstream_chat_still_retries_by_default(self) -> None:
        from opencode_go_proxy.secrets import clear_api_key_cache

        clear_api_key_cache()
        err = urllib.error.HTTPError(
            "https://mock.test/v1/chat/completions", 503, "Service Unavailable",
            {}, io.BytesIO(b'{"error":"down"}'),
        )
        payload = {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "hi"}], "stream": False}
        with mock.patch.dict(
            os.environ, {"OPENCODE_GO_API_KEY": "test-key", "OPENCODE_GO_PROXY_MAX_RETRIES": "1"}, clear=True
        ), mock.patch("urllib.request.urlopen", side_effect=err) as urlopen, pytest.raises(ProxyError):
            call_upstream_chat(payload, make_config(), "req")
        assert urlopen.call_count == 2
