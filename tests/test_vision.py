"""Unit tests for the vision caption helpers (plan 001 latency subset)."""

import base64
import io
import json
import os
import shutil
import struct
import time
import urllib.error
import urllib.request
import zlib
from http import HTTPStatus
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
    def test_auto_picks_cheapest_catalog_model(self) -> None:
        # Fixture catalog declares image input for both models; the flash
        # cost hint ranks deepseek-v4-flash cheapest.
        assert vision.resolve_caption_model("deepseek-v4-flash") == "deepseek-v4-flash"

    def test_codex_image_model_override_wins(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_IMAGE_MODEL": "mimo-v2.5"}):
            assert vision.resolve_caption_model("deepseek-v4-flash") == "mimo-v2.5"

    def test_caption_model_auto_uses_catalog_pick(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_MODEL": "auto"}):
            assert vision.resolve_caption_model("deepseek-v4-flash") == "deepseek-v4-flash"

    def test_caption_model_explicit_pins_engine(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_MODEL": "deepseek-v4-pro"}):
            assert vision.resolve_caption_model("deepseek-v4-flash") == "deepseek-v4-pro"

    def test_codex_image_model_beats_caption_model(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"CODEX_IMAGE_MODEL": "mimo-v2.5", "OPENCODE_GO_PROXY_CAPTION_MODEL": "deepseek-v4-pro"},
        ):
            assert vision.resolve_caption_model("deepseek-v4-flash") == "mimo-v2.5"

    def test_local_pin_resolves_to_local(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_MODEL": "local"}):
            assert vision.resolve_caption_model("deepseek-v4-flash") == "local"

    def test_empty_target_falls_back_to_mimo_with_empty_catalog(self, tmp_path) -> None:
        cat = tmp_path / "cat.json"
        cat.write_text(json.dumps({"models": []}))
        with mock.patch.dict(os.environ, {"CODEX_MODEL_CATALOG": str(cat)}):
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


class _FakeResponse:
    """Minimal urllib response double: context manager, status, read()."""

    def __init__(self, status: int, payload) -> None:
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


class TestEvidence:
    def test_layout_kind_when_coordinates_present(self) -> None:
        text = 'button "Save" at (120, 45); input field at (300, 200)'
        assert vision.classify_evidence(text) == "layout"

    def test_text_kind_for_transcript_shape(self) -> None:
        text = "## Summary\nA dialog.\n## Text\nHello world\n## Layout\n- ui: dialog at top\n## Uncertain\n(nothing)"
        assert vision.classify_evidence(text) == "text"

    def test_summary_kind_for_plain_description(self) -> None:
        assert vision.classify_evidence("A screenshot of a code editor") == "summary"

    def test_unreadable_kinds(self) -> None:
        assert vision.classify_evidence("") == "unreadable"
        assert vision.classify_evidence("[caption failed: boom]") == "unreadable"
        assert vision.classify_evidence("[caption unavailable]") == "unreadable"


class TestCatalogAutoPick:
    @staticmethod
    def _write_catalog(tmp_path, models) -> str:
        path = tmp_path / "cat.json"
        path.write_text(json.dumps({"models": models}))
        return str(path)

    def test_picks_cheapest_image_capable_model(self, tmp_path) -> None:
        cat = self._write_catalog(
            tmp_path,
            [
                {"slug": "mimo-v2.5", "input_modalities": ["text", "image"]},
                {"slug": "deepseek-v4-flash", "input_modalities": ["text", "image"]},
            ],
        )
        with mock.patch.dict(os.environ, {"CODEX_MODEL_CATALOG": cat}):
            assert vision.auto_pick_caption_model() == "deepseek-v4-flash"

    def test_skips_text_only_and_hidden_models(self, tmp_path) -> None:
        cat = self._write_catalog(
            tmp_path,
            [
                {"slug": "text-only", "input_modalities": ["text"]},
                {"slug": "hidden-vision", "input_modalities": ["text", "image"], "visibility": "hidden"},
                {"slug": "mimo-v2.5", "input_modalities": ["text", "image"]},
            ],
        )
        with mock.patch.dict(os.environ, {"CODEX_MODEL_CATALOG": cat}):
            assert vision.auto_pick_caption_model() == "mimo-v2.5"

    def test_empty_catalog_falls_back_to_mimo(self, tmp_path) -> None:
        cat = self._write_catalog(tmp_path, [])
        with mock.patch.dict(os.environ, {"CODEX_MODEL_CATALOG": cat}):
            assert vision.auto_pick_caption_model() == IMAGE_MODEL_DEFAULT

    def test_missing_catalog_falls_back_to_mimo(self, tmp_path) -> None:
        with mock.patch.dict(os.environ, {"CODEX_MODEL_CATALOG": str(tmp_path / "nope.json")}):
            assert vision.auto_pick_caption_model() == IMAGE_MODEL_DEFAULT


class TestEngineResolution:
    def test_auto_orders_local_before_remote_when_enabled(self) -> None:
        with mock.patch.object(vision, "local_runtime_enabled", return_value=True):
            engines = vision.resolve_engines("deepseek-v4-flash")
        assert isinstance(engines[0], vision.LocalVisionAdapter)
        assert isinstance(engines[1], vision.RemoteVisionAdapter)
        assert len(engines) == 2

    def test_auto_skips_local_when_runtime_absent(self) -> None:
        with mock.patch.object(vision, "local_runtime_enabled", return_value=False):
            engines = vision.resolve_engines("deepseek-v4-flash")
        assert len(engines) == 1
        assert isinstance(engines[0], vision.RemoteVisionAdapter)

    def test_local_pin_returns_only_local(self) -> None:
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_CAPTION_MODEL": "local"}):
            engines = vision.resolve_engines("deepseek-v4-flash")
        assert len(engines) == 1
        assert isinstance(engines[0], vision.LocalVisionAdapter)

    def test_remote_pin_returns_only_remote(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_IMAGE_MODEL": "mimo-v2.5"}):
            engines = vision.resolve_engines("deepseek-v4-flash")
        assert len(engines) == 1
        assert isinstance(engines[0], vision.RemoteVisionAdapter)
        assert engines[0].model == "mimo-v2.5"


class TestLocalProbe:
    def test_enabled_when_configured_model_present(self) -> None:
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse(200, {"data": [{"id": "qwen2.5vl:3b"}, {"id": "llama3.2:3b"}]}),
        ):
            assert vision.local_runtime_enabled() is True

    def test_disabled_when_configured_model_not_served(self) -> None:
        # A runtime serving only a vision-keyword model other than the
        # configured one must not be enabled: captions would fire against a
        # model the runtime does not have.
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse(200, {"data": [{"id": "llama3.2-vision:11b"}]}),
        ):
            assert vision.local_runtime_enabled() is False

    def test_disabled_when_only_text_models(self) -> None:
        with mock.patch(
            "urllib.request.urlopen",
            return_value=_FakeResponse(200, {"data": [{"id": "llama3.2:3b"}]}),
        ):
            assert vision.local_runtime_enabled() is False

    def test_disabled_when_runtime_absent_never_crashes(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
            assert vision.local_runtime_enabled() is False

    def test_disabled_when_payload_malformed(self) -> None:
        with mock.patch("urllib.request.urlopen", side_effect=json.JSONDecodeError("x", "y", 0)):
            assert vision.local_runtime_enabled() is False


class TestRemoteAdapter:
    def test_describe_returns_classified_evidence(self) -> None:
        chat = {
            "choices": [{"message": {"role": "assistant", "content": 'button "Save" at (120, 45)'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
        }
        with mock.patch.dict(os.environ, {"OPENCODE_GO_API_KEY": "test-key"}), mock.patch(
            "urllib.request.urlopen", return_value=_FakeResponse(200, chat)
        ):
            result, exc = vision.RemoteVisionAdapter("mimo-v2.5").describe(
                "data:image/png;base64,AAAA", vision.CAPTION_PROMPT, make_config(), "req"
            )
        assert exc is None
        assert result.kind == "layout"
        assert result.text == 'button "Save" at (120, 45)'
        assert result.model == "mimo-v2.5"
        assert result.usage["total_tokens"] == 13


class TestLocalAdapter:
    def test_describe_posts_to_local_endpoint_without_auth(self) -> None:
        chat = {
            "choices": [{"message": {"role": "assistant", "content": "A local reading"}}],
            "usage": None,
        }
        adapter = vision.LocalVisionAdapter(base_url="http://127.0.0.1:11434/v1", model="qwen2.5vl:3b")
        with mock.patch("urllib.request.urlopen", return_value=_FakeResponse(200, chat)) as urlopen:
            result, exc = adapter.describe(
                "data:image/png;base64,AAAA", vision.CAPTION_PROMPT, make_config(), "req"
            )
        assert exc is None
        assert result.kind == "summary"
        assert result.text == "A local reading"
        request = urlopen.call_args.args[0]
        assert request.full_url == "http://127.0.0.1:11434/v1/chat/completions"
        sent = json.loads(request.data)
        assert sent["model"] == "qwen2.5vl:3b"
        assert "image_url" in sent["messages"][0]["content"][1]

    def test_describe_failure_maps_to_proxy_error(self) -> None:
        adapter = vision.LocalVisionAdapter(base_url="http://127.0.0.1:1234/v1", model="llama3.2-vision:11b")
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            result, exc = adapter.describe(
                "data:image/png;base64,AAAA", vision.CAPTION_PROMPT, make_config(), "req"
            )
        assert result is None
        assert isinstance(exc, ProxyError)
        assert exc.status == HTTPStatus.BAD_GATEWAY


class TestDescribeMetering:
    @staticmethod
    def _usage_events() -> list[dict]:
        from opencode_go_proxy.meter import usage_events_path

        path = usage_events_path()
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    class _OkAdapter:
        model = "fake-vision"

        def describe(self, image_url, prompt, config, request_id):
            return vision.AdapterResult(
                kind="summary",
                text="cached-me",
                model="fake-vision",
                usage={"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
            ), None

    class _FailingAdapter:
        model = "bad-vision"

        def describe(self, image_url, prompt, config, request_id):
            return None, ProxyError(HTTPStatus.BAD_GATEWAY, "upstream exploded")

    def test_miss_meters_kind_vision_and_caches(self) -> None:
        image = "data:image/png;base64,AAAA"
        first = vision.describe(image, engines=[self._OkAdapter()], request_id="req-1")
        assert first.kind == "summary"
        assert first.text == "cached-me"
        assert first.cached is False
        events = self._usage_events()
        assert len(events) == 1
        assert events[0]["kind"] == "vision"
        assert events[0]["model"] == "fake-vision"
        assert events[0]["status"] == 200
        assert events[0]["totalTokens"] == 13

        second = vision.describe(image, engines=[self._OkAdapter()], request_id="req-2")
        assert second.cached is True
        assert len(self._usage_events()) == 1

    def test_total_failure_meters_unreadable(self) -> None:
        evidence = vision.describe("data:image/png;base64,BBBB", engines=[self._FailingAdapter()], request_id="req-3")
        assert evidence.kind == "unreadable"
        assert "[caption failed" in evidence.text
        events = self._usage_events()
        assert len(events) == 1
        assert events[0]["kind"] == "vision"
        assert events[0]["status"] == 502
        assert events[0]["model"] == "bad-vision"

    def test_falls_back_to_next_engine(self) -> None:
        class SecondAdapter:
            model = "ok-vision"

            def describe(self, image_url, prompt, config, request_id):
                return vision.AdapterResult(kind="summary", text="saved by second", model="ok-vision"), None

        evidence = vision.describe(
            "data:image/png;base64,CCCC", engines=[self._FailingAdapter(), SecondAdapter()], request_id="req-4"
        )
        assert evidence.text == "saved by second"
        assert evidence.model == "ok-vision"
        events = self._usage_events()
        assert len(events) == 1
        assert events[0]["kind"] == "vision"
        assert events[0]["model"] == "ok-vision"
