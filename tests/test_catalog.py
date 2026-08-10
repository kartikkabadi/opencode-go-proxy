import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Self
from unittest import mock

from opencode_go_proxy.catalog import (
    CatalogDiscoveryError,
    _model_from_discovery,
    discover_models,
    load_known_slugs,
    merge_models,
    model_messages,
    render_full_catalog,
)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


class DiscoverModelsTests(unittest.TestCase):
    def test_discover_models_returns_only_opencode_entries(self) -> None:
        payload = {
            "opencode": {
                "models": {
                    "gpt-5.5": {"id": "gpt-5.5", "name": "GPT-5.5", "reasoning": True},
                    "deepseek-v4-flash": {
                        "id": "deepseek-v4-flash",
                        "name": "DeepSeek V4 Flash",
                        "limit": {"context": 400000},
                    },
                }
            },
            "other-provider": {
                "models": {
                    "some-model": {"id": "some-model", "name": "Some Model"},
                }
            },
        }
        with mock.patch("opencode_go_proxy.catalog.urllib.request.urlopen", return_value=FakeResponse(payload)):
            models = discover_models()

        self.assertEqual([m["id"] for m in models], ["gpt-5.5", "deepseek-v4-flash"])
        self.assertNotIn("some-model", {m["id"] for m in models})

    def test_discover_models_raises_on_urlopen_failure(self) -> None:
        with mock.patch(
            "opencode_go_proxy.catalog.urllib.request.urlopen",
            side_effect=urllib.error.URLError("boom"),
        ), self.assertRaises(CatalogDiscoveryError):
            discover_models()


class MergeModelsTests(unittest.TestCase):
    def test_existing_plus_new_returns_new_only(self) -> None:
        discovered = [
            {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash"},
            {"id": "new-model", "name": "New Model"},
        ]
        new_models, removed = merge_models({"deepseek-v4-flash"}, discovered)

        self.assertEqual([m["id"] for m in new_models], ["new-model"])
        self.assertEqual(removed, [])

    def test_stale_existing_slug_is_removed(self) -> None:
        discovered = [{"id": "current-model", "name": "Current"}]
        new_models, removed = merge_models({"current-model", "retired-model"}, discovered)

        self.assertEqual(new_models, [])
        self.assertEqual(removed, ["retired-model"])

    def test_disjoint_sets_are_handled(self) -> None:
        discovered = [{"id": "brand-new", "name": "Brand New"}]
        new_models, removed = merge_models({"old-known"}, discovered)

        self.assertEqual([m["id"] for m in new_models], ["brand-new"])
        self.assertEqual(removed, ["old-known"])


class LoadKnownSlugsTests(unittest.TestCase):
    def test_load_known_slugs_reads_temp_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog_path = Path(tmp) / "catalog.json"
            catalog_path.write_text(
                json.dumps({"models": [{"slug": "deepseek-v4-flash"}, {"slug": "deepseek-v4-pro"}]})
            )

            slugs = load_known_slugs(catalog_path=str(catalog_path))

        self.assertEqual(slugs, {"deepseek-v4-flash", "deepseek-v4-pro"})

    def test_load_known_slugs_returns_empty_set_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.json"

            slugs = load_known_slugs(catalog_path=str(missing))

        self.assertEqual(slugs, set())


def minimal_compact(shared_instructions: str, model: dict) -> dict:
    return {
        "fetched_at": "2026-08-10T12:00:00.000000Z",
        "etag": 'W/"opencode-go-models-test"',
        "shared_instructions": shared_instructions,
        "client_version": "0.147.0",
        "models": [model],
    }


def minimal_model(**overrides: object) -> dict:
    record = {
        "slug": "cold-start-model",
        "display_name": "Cold Start Model",
        "description": "Rendered on first discovery.",
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [{"effort": "low", "description": "Lighter"}],
        "context_window": 1000000,
        "max_context_window": 1000000,
    }
    record.update(overrides)
    return record


class ColdStartRenderTests(unittest.TestCase):
    def test_render_with_empty_shared_instructions_does_not_crash(self) -> None:
        compact = minimal_compact("", minimal_model())

        rendered = render_full_catalog(compact)

        model = rendered["models"][0]
        self.assertEqual(model["base_instructions"], "")
        template = model["model_messages"]["instructions_template"]
        self.assertIn("You are Codex, a coding agent based on Cold Start Model.", template)

    def test_model_messages_with_empty_shared_instructions_uses_identity_line(self) -> None:
        messages = model_messages("", "Cold Start Model", 1000000)

        self.assertEqual(
            messages["instructions_template"],
            "You are Codex, a coding agent based on Cold Start Model. You and the user "
            "share one workspace, and your job is to collaborate with them until their "
            "goal is genuinely handled.",
        )
        self.assertEqual(messages["auto_compact_token_limit"], 900000)

    def test_discovery_without_limit_context_keeps_default(self) -> None:
        record = _model_from_discovery({"id": "no-context-model", "name": "No Context"})

        self.assertEqual(record["context_window"], 1000000)
        self.assertEqual(record["max_context_window"], 1000000)

    def test_render_with_default_context_window_does_not_crash(self) -> None:
        discovered = _model_from_discovery({"id": "no-context-model", "name": "No Context"})
        compact = minimal_compact("Line one\n", discovered)

        rendered = render_full_catalog(compact)

        self.assertEqual(
            rendered["models"][0]["model_messages"]["auto_compact_token_limit"],
            round(1000000 * 0.9),
        )


if __name__ == "__main__":
    unittest.main()
