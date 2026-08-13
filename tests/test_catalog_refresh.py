import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

from opencode_go_proxy.catalog import (
    CatalogDiscoveryError,
    _model_from_discovery,
    refresh_catalog,
)


def make_compact(fetched_at: str) -> dict:
    return {
        "fetched_at": fetched_at,
        "etag": 'W/"opencode-go-models-test"',
        "shared_instructions": "Line one\nLine two\n",
        "client_version": "0.147.0",
        "models": [
            {
                "slug": "existing-model",
                "display_name": "Existing Model",
                "description": "Already known.",
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": [{"effort": "low", "description": "Lighter"}],
                "context_window": 1000000,
                "max_context_window": 1000000,
                "effective_context_window_percent": 95,
                "apply_patch_tool_type": "freeform",
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 50,
                "additional_speed_tiers": [],
                "service_tiers": [],
                "availability_nux": None,
                "upgrade": None,
                "supports_reasoning_summaries": True,
                "default_reasoning_summary": "none",
                "support_verbosity": False,
                "default_verbosity": "low",
                "web_search_tool_type": "text_and_image",
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                "supports_parallel_tool_calls": True,
                "supports_image_detail_original": False,
                "experimental_supported_tools": [],
                "input_modalities": ["text", "image"],
                "supports_search_tool": False,
                "use_responses_lite": False,
            }
        ],
    }


class RefreshCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.compact_path = os.path.join(self.tmp, "models.json")
        self.catalog_path = os.path.join(self.tmp, "catalog.json")
        self.now = datetime.datetime(2026, 8, 10, 12, 0, 0, tzinfo=datetime.UTC)

    def _write_compact(self, fetched_at: str) -> None:
        with open(self.compact_path, "w") as f:
            json.dump(make_compact(fetched_at), f)

    def test_fresh_compact_skips_discovery(self) -> None:
        self._write_compact((self.now - datetime.timedelta(hours=1)).isoformat())

        def fail_if_called(*args, **kwargs):
            raise AssertionError("discover_models must not be called for a fresh catalog")

        with mock.patch("opencode_go_proxy.catalog.discover_models", side_effect=fail_if_called):
            rendered = refresh_catalog(
                compact_path=self.compact_path,
                catalog_path=self.catalog_path,
                now=self.now,
            )

        self.assertEqual(rendered["models"][0]["slug"], "existing-model")
        with open(self.compact_path) as f:
            compact = json.load(f)
        self.assertNotEqual(compact["fetched_at"], self.now.isoformat())
        self.assertTrue(os.path.exists(self.catalog_path))

    def test_stale_compact_refreshes_and_adds_model(self) -> None:
        self._write_compact((self.now - datetime.timedelta(hours=25)).isoformat())
        discovered = [
            {
                "id": "new-model",
                "name": "New Model",
                "description": "Freshly discovered.",
                "limit": {"context": 200000},
                "modalities": ["text"],
            }
        ]

        with mock.patch("opencode_go_proxy.catalog.discover_models", return_value=discovered):
            rendered = refresh_catalog(
                compact_path=self.compact_path,
                catalog_path=self.catalog_path,
                now=self.now,
            )

        self.assertEqual([m["slug"] for m in rendered["models"]], ["existing-model", "new-model"])
        with open(self.compact_path) as f:
            compact = json.load(f)
        self.assertEqual(compact["fetched_at"], self.now.isoformat())
        self.assertEqual(compact["models"][1]["slug"], "new-model")

    def test_force_refreshes_fresh_compact(self) -> None:
        self._write_compact((self.now - datetime.timedelta(hours=1)).isoformat())

        with mock.patch("opencode_go_proxy.catalog.discover_models", return_value=[]):
            rendered = refresh_catalog(
                compact_path=self.compact_path,
                catalog_path=self.catalog_path,
                now=self.now,
                force=True,
            )

        with open(self.compact_path) as f:
            compact = json.load(f)
        self.assertEqual(compact["fetched_at"], self.now.isoformat())
        self.assertEqual(rendered["models"][0]["slug"], "existing-model")

    def test_refresh_disabled_by_env(self) -> None:
        self._write_compact((self.now - datetime.timedelta(hours=25)).isoformat())

        def fail_if_called(*args, **kwargs):
            raise AssertionError("discover_models must not be called when refresh is disabled")

        with mock.patch.dict(os.environ, {"OPENCODE_GO_CATALOG_REFRESH": "0"}), mock.patch(
            "opencode_go_proxy.catalog.discover_models", side_effect=fail_if_called
        ):
            rendered = refresh_catalog(
                compact_path=self.compact_path,
                catalog_path=self.catalog_path,
                now=self.now,
            )

        self.assertEqual(rendered["models"][0]["slug"], "existing-model")
        with open(self.compact_path) as f:
            compact = json.load(f)
        self.assertNotEqual(compact["fetched_at"], self.now.isoformat())

    def test_offline_with_existing_compact_uses_fallback(self) -> None:
        self._write_compact((self.now - datetime.timedelta(hours=25)).isoformat())

        with mock.patch(
            "opencode_go_proxy.catalog.discover_models",
            side_effect=CatalogDiscoveryError("offline"),
        ):
            rendered = refresh_catalog(
                compact_path=self.compact_path,
                catalog_path=self.catalog_path,
                now=self.now,
            )

        self.assertEqual(rendered["models"][0]["slug"], "existing-model")
        with open(self.compact_path) as f:
            compact = json.load(f)
        self.assertNotEqual(compact["fetched_at"], self.now.isoformat())

    def test_offline_without_compact_raises(self) -> None:
        with mock.patch(
            "opencode_go_proxy.catalog.discover_models",
            side_effect=CatalogDiscoveryError("offline"),
        ), self.assertRaises(CatalogDiscoveryError):
            refresh_catalog(
                compact_path=self.compact_path,
                catalog_path=self.catalog_path,
                now=self.now,
            )

    def test_model_from_discovery_maps_fields(self) -> None:
        record = _model_from_discovery(
            {
                "id": "gpt-test",
                "name": "GPT Test",
                "description": "A test model.",
                "limit": {"context": 400000},
                "modalities": ["text", "image"],
                "reasoning": {"efforts": ["low", "high"]},
            }
        )

        self.assertEqual(record["slug"], "gpt-test")
        self.assertEqual(record["display_name"], "GPT Test")
        self.assertEqual(record["description"], "A test model.")
        self.assertEqual(record["context_window"], 400000)
        self.assertEqual(record["max_context_window"], 400000)
        self.assertEqual(record["input_modalities"], ["text", "image"])
        self.assertEqual(record["default_reasoning_level"], "low")
        self.assertEqual([r["effort"] for r in record["supported_reasoning_levels"]], ["low", "high"])


if __name__ == "__main__":
    unittest.main()
