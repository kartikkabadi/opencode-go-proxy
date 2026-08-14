"""Zen catalog: family resolution, capture TTL/fallback, and merged-catalog wiring.

All network is mocked; the real opencode.ai and models.dev endpoints are
never touched.
"""

import datetime
import json
import os
import unittest
import urllib.error
from typing import Self
from unittest import mock

from opencode_go_proxy import catalog, zen_catalog
from opencode_go_proxy.meter import state_dir

ZEN_PAYLOAD = {
    "object": "list",
    "data": [
        {"id": "claude-sonnet-4-5", "object": "model", "created": 1, "owned_by": "zen"},
        {"id": "gemini-3-pro", "object": "model", "created": 1, "owned_by": "zen"},
        {"id": "gpt-5.5", "object": "model", "created": 1, "owned_by": "zen"},
        {"id": "grok-4.5", "object": "model", "created": 1, "owned_by": "zen"},
        {"id": "deepseek-v4-flash", "object": "model", "created": 1, "owned_by": "zen"},
        {"id": "qwen3-coder", "object": "model", "created": 1, "owned_by": "zen"},
    ],
}

# Realistic models.dev family values for the opencode provider, including the
# qwen case that must NOT override the prefix rules.
MODELS_DEV_PAYLOAD = {
    "opencode": {
        "models": {
            "claude-sonnet-4-5": {"family": "claude-sonnet"},
            "gemini-3-pro": {"family": "gemini-pro"},
            "gpt-5.5": {"family": "gpt"},
            "grok-4.5": {"family": "grok"},
            "deepseek-v4-flash": {"family": "deepseek-flash"},
            "qwen3-coder": {"family": "qwen"},
        }
    }
}

NOW = datetime.datetime(2026, 8, 14, 12, 0, 0, tzinfo=datetime.UTC)


class FakeResponse:
    """Minimal urllib response: json.load(resp) calls resp.read()."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status = 200

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def fake_urlopen(payloads: dict[str, dict]):
    """urlopen side effect routing by URL substring; anything else fails the test."""

    def _open(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        for marker, payload in payloads.items():
            if marker in url:
                return FakeResponse(payload)
        raise AssertionError(f"unexpected urlopen call: {url}")

    return _open


def _clear_caches() -> None:
    zen_catalog._ZEN_MODELS_CACHE = None
    zen_catalog._ZEN_FAMILIES_CACHE = None


def _seed_zen_cache(entries: list[dict], families: dict[str, str] | None = None) -> None:
    """Write zen cache files directly so merged-catalog tests need no network."""
    state = state_dir()
    os.makedirs(state, exist_ok=True)
    with open(zen_catalog.zen_models_path(), "w") as handle:
        json.dump({"fetched_at": "2026-08-14T00:00:00+00:00", "models": entries}, handle)
    if families is None:
        families = {entry["id"]: zen_catalog.resolve_family(entry["id"]) for entry in entries}
    with open(zen_catalog.zen_catalog_path(), "w") as handle:
        json.dump(
            {"version": 1, "fetched_at": "2026-08-14T00:00:00+00:00",
             "models": {model_id: {"family": family} for model_id, family in sorted(families.items())}},
            handle,
        )
    _clear_caches()


class ResolveFamilyTests(unittest.TestCase):
    def test_prefix_fallback_rules(self) -> None:
        cases = {
            "claude-sonnet-4-5": "anthropic_messages",
            "claude-opus-5": "anthropic_messages",
            "qwen3-coder": "anthropic_messages",
            "gemini-3-pro": "google_gemini",
            "gpt-5.5": "openai_responses",
            "grok-4.5": "openai_responses",
            "deepseek-v4-flash": "openai_chat",
            "glm-4.6": "openai_chat",
            "minimax-m3": "openai_chat",
            "kimi-k2": "openai_chat",
            "deepseek-v4-flash-free": "openai_chat",
            "big-pickle": "openai_chat",
        }
        for model_id, expected in cases.items():
            with self.subTest(model_id=model_id):
                self.assertEqual(zen_catalog.resolve_family(model_id), expected)

    def test_generic_models_dev_family_overrides_prefix(self) -> None:
        # models.dev classifies deepseek-v4-flash as anthropic here; the
        # metadata wins over the prefix fallback.
        families = {"deepseek-v4-flash": "anthropic"}
        self.assertEqual(zen_catalog.resolve_family("deepseek-v4-flash", families), "anthropic_messages")

    def test_specific_models_dev_families_classify_by_prefix(self) -> None:
        families = {
            "claude-sonnet-4-5": "claude-sonnet",
            "gemini-3-pro": "gemini-pro",
            "gpt-5.5": "gpt-codex",
            "grok-4.5": "grok",
        }
        self.assertEqual(zen_catalog.resolve_family("claude-sonnet-4-5", families), "anthropic_messages")
        self.assertEqual(zen_catalog.resolve_family("gemini-3-pro", families), "google_gemini")
        self.assertEqual(zen_catalog.resolve_family("gpt-5.5", families), "openai_responses")
        self.assertEqual(zen_catalog.resolve_family("grok-4.5", families), "openai_responses")

    def test_unmapped_models_dev_family_keeps_prefix_fallback(self) -> None:
        # models.dev groups qwen with the compatible crowd; zen still routes
        # qwen through the anthropic messages surface, so the metadata must
        # not override the prefix rules here.
        families = {"qwen3-coder": "qwen"}
        self.assertEqual(zen_catalog.resolve_family("qwen3-coder", families), "anthropic_messages")

    def test_empty_models_dev_map_falls_back(self) -> None:
        self.assertEqual(zen_catalog.resolve_family("gemini-3-pro", {}), "google_gemini")


class CaptureZenModelsTests(unittest.TestCase):
    def setUp(self) -> None:
        os.makedirs(state_dir(), exist_ok=True)
        _clear_caches()

    def tearDown(self) -> None:
        _clear_caches()

    def test_capture_success_persists_cache_and_families(self) -> None:
        urlopen = fake_urlopen({"zen/v1/models": ZEN_PAYLOAD, "models.dev": MODELS_DEV_PAYLOAD})
        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            result = zen_catalog.capture_zen_models(now=NOW)

        self.assertEqual(result["fetched_at"], NOW.isoformat())
        self.assertEqual(len(result["models"]), 6)
        self.assertEqual(
            zen_catalog.zen_model_ids(),
            {"claude-sonnet-4-5", "gemini-3-pro", "gpt-5.5", "grok-4.5", "deepseek-v4-flash", "qwen3-coder"},
        )
        families = zen_catalog.zen_families()
        self.assertEqual(families["claude-sonnet-4-5"], "anthropic_messages")
        self.assertEqual(families["gemini-3-pro"], "google_gemini")
        self.assertEqual(families["gpt-5.5"], "openai_responses")
        self.assertEqual(families["grok-4.5"], "openai_responses")
        self.assertEqual(families["deepseek-v4-flash"], "openai_chat")
        self.assertEqual(families["qwen3-coder"], "anthropic_messages")
        self.assertTrue(os.path.exists(zen_catalog.zen_models_path()))
        self.assertTrue(os.path.exists(zen_catalog.zen_catalog_path()))

    def test_fresh_cache_skips_network(self) -> None:
        _seed_zen_cache(ZEN_PAYLOAD["data"])

        def fail_if_called(*args, **kwargs) -> None:
            raise AssertionError("capture must not fetch when the cache is fresh")

        with mock.patch("urllib.request.urlopen", side_effect=fail_if_called):
            result = zen_catalog.capture_zen_models(now=NOW)
        self.assertEqual(len(result["models"]), 6)

    def test_stale_cache_retries_once_then_falls_back(self) -> None:
        with open(zen_catalog.zen_models_path(), "w") as handle:
            json.dump(
                {"fetched_at": "2026-08-01T00:00:00+00:00", "models": ZEN_PAYLOAD["data"][:2]},
                handle,
            )
        _clear_caches()
        calls: list[str] = []

        def failing_urlopen(request, timeout=None):
            calls.append(str(request))
            raise urllib.error.URLError("offline")

        with mock.patch("urllib.request.urlopen", side_effect=failing_urlopen):
            result = zen_catalog.capture_zen_models(now=NOW)

        self.assertEqual(len(calls), 2)  # first attempt plus one retry
        self.assertEqual(result["fetched_at"], "2026-08-01T00:00:00+00:00")
        self.assertEqual([e["id"] for e in result["models"]], ["claude-sonnet-4-5", "gemini-3-pro"])
        self.assertEqual(zen_catalog.zen_model_ids(), {"claude-sonnet-4-5", "gemini-3-pro"})

    def test_fetch_failure_with_nothing_cached_returns_empty(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("offline")
        ):
            result = zen_catalog.capture_zen_models(now=NOW)
        self.assertEqual(result["models"], [])
        self.assertEqual(zen_catalog.zen_model_ids(), set())
        self.assertFalse(os.path.exists(zen_catalog.zen_models_path()))

    def test_refresh_disabled_by_env(self) -> None:
        _seed_zen_cache(ZEN_PAYLOAD["data"][:2])

        def fail_if_called(*args, **kwargs) -> None:
            raise AssertionError("capture must not fetch when refresh is disabled")

        with mock.patch.dict(os.environ, {zen_catalog.ZEN_MODELS_REFRESH_ENV: "0"}), mock.patch(
            "urllib.request.urlopen", side_effect=fail_if_called
        ):
            result = zen_catalog.capture_zen_models(now=NOW)
        self.assertEqual(len(result["models"]), 2)


class ZenMtimeCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        os.makedirs(state_dir(), exist_ok=True)
        _clear_caches()

    def tearDown(self) -> None:
        _clear_caches()

    def test_zen_model_ids_refresh_on_mtime_change(self) -> None:
        path = zen_catalog.zen_models_path()
        with open(path, "w") as handle:
            json.dump({"fetched_at": "2026-08-14T00:00:00+00:00", "models": [{"id": "gpt-5.5"}]}, handle)
        self.assertEqual(zen_catalog.zen_model_ids(), {"gpt-5.5"})

        old_mtime = os.stat(path).st_mtime_ns
        with open(path, "w") as handle:
            json.dump(
                {"fetched_at": "2026-08-14T00:00:00+00:00",
                 "models": [{"id": "gpt-5.5"}, {"id": "claude-sonnet-4-5"}]},
                handle,
            )
        os.utime(path, ns=(old_mtime + 10**9, old_mtime + 10**9))
        # Same path, newer content: the mtime cache must re-read.
        self.assertEqual(zen_catalog.zen_model_ids(), {"gpt-5.5", "claude-sonnet-4-5"})

    def test_zen_families_refresh_on_mtime_change(self) -> None:
        entries = [{"id": "gpt-5.5"}, {"id": "gemini-3-pro"}, {"id": "new-model"}]
        _seed_zen_cache(
            entries,
            families={"gpt-5.5": "openai_responses", "gemini-3-pro": "google_gemini"},
        )
        families = zen_catalog.zen_families()
        self.assertEqual(families["gpt-5.5"], "openai_responses")
        # Not in the persisted map: resolved on the fly by prefix rules.
        self.assertEqual(families["new-model"], "openai_chat")

        catalog_path = zen_catalog.zen_catalog_path()
        old_mtime = os.stat(catalog_path).st_mtime_ns
        with open(catalog_path, "w") as handle:
            json.dump(
                {"version": 1, "fetched_at": "2026-08-14T00:00:00+00:00",
                 "models": {"gpt-5.5": {"family": "anthropic_messages"},
                            "gemini-3-pro": {"family": "google_gemini"}}},
                handle,
            )
        os.utime(catalog_path, ns=(old_mtime + 10**9, old_mtime + 10**9))
        self.assertEqual(zen_catalog.zen_families()["gpt-5.5"], "anthropic_messages")


class ZenMergedCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        os.makedirs(state_dir(), exist_ok=True)
        _clear_caches()

    def tearDown(self) -> None:
        _clear_caches()

    def test_merged_catalog_contains_zen_records(self) -> None:
        _seed_zen_cache(ZEN_PAYLOAD["data"])
        merged = catalog.render_merged_catalog()

        zen_entries = [m for m in merged["models"] if str(m["slug"]).startswith("zen/")]
        self.assertEqual(len(zen_entries), 6)
        slugs = {str(m["slug"]) for m in merged["models"]}
        self.assertIn("zen/claude-sonnet-4-5", slugs)
        self.assertIn("deepseek-v4-flash", slugs)  # go entry still present

        record = next(m for m in zen_entries if m["slug"] == "zen/claude-sonnet-4-5")
        self.assertEqual(record["family"], "anthropic_messages")
        self.assertIsNone(record["availability_nux"])  # object form: dict or null, never a bare string
        self.assertEqual(record["visibility"], "list")
        self.assertEqual(record["display_name"], "claude-sonnet-4-5")
        self.assertEqual(record["comp_hash"], "opencode-go-zen-claude-sonnet-4-5-v1")
        self.assertIn("instructions_template", record["model_messages"])
        for key in catalog.CANONICAL_MODEL_KEYS:
            self.assertIn(key, record)

    def test_zen_display_name_suffixed_when_go_record_shares_bare_id(self) -> None:
        _seed_zen_cache(ZEN_PAYLOAD["data"])
        merged = catalog.render_merged_catalog()

        record = next(m for m in merged["models"] if m["slug"] == "zen/deepseek-v4-flash")
        self.assertEqual(record["display_name"], "deepseek-v4-flash (Zen)")

    def test_zen_display_name_suffixed_when_go_record_shares_display_name(self) -> None:
        _seed_zen_cache(ZEN_PAYLOAD["data"])
        compact = {
            "fetched_at": "2026-08-14T00:00:00Z",
            "etag": "",
            "client_version": "0.147.0",
            "shared_instructions": "",
            "models": [{"slug": "some-go-model", "display_name": "claude-sonnet-4-5"}],
        }
        with open(catalog.state_compact_path(), "w") as handle:
            json.dump(compact, handle)

        merged = catalog.render_merged_catalog()

        record = next(m for m in merged["models"] if m["slug"] == "zen/claude-sonnet-4-5")
        self.assertEqual(record["display_name"], "claude-sonnet-4-5 (Zen)")

    def test_zen_only_record_keeps_plain_display_name(self) -> None:
        # claude-sonnet-4-5 has no opencode-go counterpart in the seed
        # catalog, so its name stays unambiguous.
        _seed_zen_cache(ZEN_PAYLOAD["data"])
        merged = catalog.render_merged_catalog()

        record = next(m for m in merged["models"] if m["slug"] == "zen/claude-sonnet-4-5")
        self.assertEqual(record["display_name"], "claude-sonnet-4-5")

    def test_disambiguation_leaves_slug_keys_unchanged(self) -> None:
        _seed_zen_cache(ZEN_PAYLOAD["data"])
        merged = catalog.render_merged_catalog()

        zen_slugs = {
            str(m["slug"]) for m in merged["models"] if str(m["slug"]).startswith("zen/")
        }
        self.assertEqual(
            zen_slugs,
            {
                "zen/claude-sonnet-4-5",
                "zen/deepseek-v4-flash",
                "zen/gemini-3-pro",
                "zen/gpt-5.5",
                "zen/grok-4.5",
                "zen/qwen3-coder",
            },
        )

    def test_merged_catalog_without_zen_capture_has_no_zen_records(self) -> None:
        merged = catalog.render_merged_catalog()
        self.assertFalse(any(str(m["slug"]).startswith("zen/") for m in merged["models"]))

    def test_merged_models_serve_zen_slugs_via_v1_models_shape(self) -> None:
        _seed_zen_cache(ZEN_PAYLOAD["data"])
        catalog.render_merged_catalog()

        # The exact shape app.py builds for /v1/models.
        slugs = catalog.merged_model_slugs()
        shape = {"object": "list", "data": [{"id": slug, "object": "model"} for slug in slugs]}
        ids = {entry["id"] for entry in shape["data"]}
        self.assertEqual(len(shape["data"]), len(slugs))
        self.assertEqual(shape["object"], "list")
        zen_ids = {model_id for model_id in ids if model_id.startswith("zen/")}
        self.assertIn("zen/gpt-5.5", zen_ids)
        self.assertIn("zen/qwen3-coder", zen_ids)
        # Every zen slug carries the prefix; go and native slugs stay bare.
        self.assertTrue(all(model_id.startswith("zen/") for model_id in zen_ids))


if __name__ == "__main__":
    unittest.main()
