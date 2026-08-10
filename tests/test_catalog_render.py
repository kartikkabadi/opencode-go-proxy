import json
import tempfile
import unittest
from pathlib import Path

from opencode_go_proxy.catalog import (
    CatalogDiscoveryError,
    load_compact,
    render_full_catalog,
    write_catalog,
)


def compact_catalog() -> dict:
    return {
        "fetched_at": "2026-06-22T10:18:00.000000Z",
        "etag": 'W/"opencode-go-catalog-v0.1.2"',
        "shared_instructions": "Line one\nLine two\nLine three\n",
        "client_version": "0.147.0",
        "models": [
            {
                "slug": "deepseek-v4-flash",
                "display_name": "DeepSeek V4 Flash",
                "description": "Fast and cheap.",
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": [{"effort": "low", "description": "Lighter"}],
                "context_window": 1000000,
                "max_context_window": 1000000,
                "effective_context_window_percent": 95,
                "apply_patch_tool_type": "freeform",
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 10,
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
            },
            {
                "slug": "deepseek-v4-pro",
                "display_name": "DeepSeek V4 Pro",
                "description": "Stronger reasoning.",
                "default_reasoning_level": "max",
                "supported_reasoning_levels": [{"effort": "max", "description": "Deepest"}],
                "context_window": 400000,
                "max_context_window": 400000,
                "effective_context_window_percent": 95,
                "apply_patch_tool_type": "freeform",
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 20,
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
            },
        ],
    }


class RenderFullCatalogTests(unittest.TestCase):
    def test_renders_one_full_model_per_compact_model(self) -> None:
        rendered = render_full_catalog(compact_catalog())

        self.assertEqual([m["slug"] for m in rendered["models"]], ["deepseek-v4-flash", "deepseek-v4-pro"])
        for model in rendered["models"]:
            self.assertEqual(model["base_instructions"], "Line one\nLine two\nLine three\n")
            self.assertIn("model_messages", model)

    def test_model_messages_has_shared_instructions_and_compact_fields(self) -> None:
        rendered = render_full_catalog(compact_catalog())

        flash = rendered["models"][0]
        self.assertIn("Line two\nLine three\n", flash["model_messages"]["instructions_template"])
        self.assertIn("You are Codex, a coding agent based on DeepSeek V4 Flash.", flash["model_messages"]["instructions_template"])
        self.assertTrue(flash["supports_reasoning_summaries"])
        self.assertEqual(flash["auto_compact_token_limit"], round(1000000 * 0.9))
        self.assertEqual(flash["multi_agent_version"], "v1")
        self.assertNotIn("multi_agent_version", flash["model_messages"])
        self.assertNotIn("auto_compact_token_limit", flash["model_messages"])
        self.assertNotIn("supports_reasoning_summaries", flash["model_messages"])

        pro = rendered["models"][1]
        self.assertEqual(pro["auto_compact_token_limit"], round(400000 * 0.9))
        self.assertEqual(pro["multi_agent_version"], "v1")

    def test_identity_line_is_replaced_per_model(self) -> None:
        rendered = render_full_catalog(compact_catalog())

        self.assertTrue(
            rendered["models"][0]["model_messages"]["instructions_template"].startswith(
                "You are Codex, a coding agent based on DeepSeek V4 Flash."
            )
        )
        self.assertTrue(
            rendered["models"][1]["model_messages"]["instructions_template"].startswith(
                "You are Codex, a coding agent based on DeepSeek V4 Pro."
            )
        )
        self.assertNotIn("Line one\n", rendered["models"][0]["model_messages"]["instructions_template"].splitlines()[0])

    def test_input_dict_is_not_mutated(self) -> None:
        compact = compact_catalog()
        before = json.dumps(compact, sort_keys=True)

        render_full_catalog(compact)

        self.assertEqual(json.dumps(compact, sort_keys=True), before)
        for model in compact["models"]:
            self.assertNotIn("base_instructions", model)
            self.assertNotIn("model_messages", model)

    def test_rendered_top_level_metadata_is_copied(self) -> None:
        rendered = render_full_catalog(compact_catalog())

        self.assertEqual(rendered["fetched_at"], "2026-06-22T10:18:00.000000Z")
        self.assertEqual(rendered["etag"], 'W/"opencode-go-catalog-v0.1.2"')
        self.assertEqual(rendered["client_version"], "0.147.0")


class LoadCompactTests(unittest.TestCase):
    def test_load_compact_reads_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "compact.json"
            path.write_text(json.dumps(compact_catalog()))

            loaded = load_compact(str(path))

        self.assertEqual(loaded["shared_instructions"], "Line one\nLine two\nLine three\n")
        self.assertEqual(len(loaded["models"]), 2)

    def test_load_compact_raises_on_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json")

            with self.assertRaises(CatalogDiscoveryError):
                load_compact(str(path))

    def test_load_compact_raises_on_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.json"

            with self.assertRaises(CatalogDiscoveryError):
                load_compact(str(missing))


class WriteCatalogTests(unittest.TestCase):
    def test_write_catalog_round_trips(self) -> None:
        compact = compact_catalog()
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "full.json"

            write_catalog(compact, str(out_path))

            with open(out_path) as f:
                reloaded = json.load(f)

        self.assertEqual([m["slug"] for m in reloaded["models"]], ["deepseek-v4-flash", "deepseek-v4-pro"])
        self.assertEqual(reloaded["models"][0]["base_instructions"], "Line one\nLine two\nLine three\n")
        self.assertEqual(
            reloaded["models"][0]["auto_compact_token_limit"],
            round(1000000 * 0.9),
        )
        self.assertEqual(reloaded["models"][0]["multi_agent_version"], "v1")
        self.assertNotIn("shared_instructions", reloaded)
        self.assertEqual(json.dumps(compact, sort_keys=True), json.dumps(compact_catalog(), sort_keys=True))


class CanonicalContractTests(unittest.TestCase):
    """Plan 003: the renderer must match the exact key set Codex reads.

    The fixture is a checked-in sample of ~/.codex/codex-router/merged-models.json;
    parity is checked against the fixture, never against the module constants, so
    a drift in either direction fails.
    """

    FIXTURE = Path(__file__).parent / "fixtures" / "merged-model-keys.json"

    def _fixture(self) -> dict:
        with open(self.FIXTURE) as f:
            return json.load(f)

    def test_rendered_key_set_matches_merged_catalog_fixture(self) -> None:
        fixture = self._fixture()
        model = render_full_catalog(compact_catalog())["models"][0]

        self.assertEqual(set(model.keys()), set(fixture["top_level_keys"]))
        self.assertEqual(set(model["model_messages"].keys()), set(fixture["model_messages_keys"]))

    def test_multi_agent_version_is_top_level(self) -> None:
        rendered = render_full_catalog(compact_catalog())

        for model in rendered["models"]:
            self.assertEqual(model["multi_agent_version"], "v1")
            self.assertNotIn("multi_agent_version", model["model_messages"])
            self.assertEqual(model["auto_compact_token_limit"], round(model["context_window"] * 0.9))

    def test_visibility_and_availability_flags_are_rendered(self) -> None:
        compact = compact_catalog()
        compact["models"][0]["availability_nux"] = {"message": "Introducing Flash"}

        model = render_full_catalog(compact)["models"][0]

        self.assertEqual(model["visibility"], "list")
        self.assertEqual(model["availability_nux"], {"message": "Introducing Flash"})
        self.assertIs(model["supported_in_api"], True)

    def test_optional_canonical_keys_are_not_dropped(self) -> None:
        compact = compact_catalog()
        compact["models"][0]["tool_mode"] = "code_mode_only"
        compact["models"][0]["include_plugin_usage_instructions"] = False
        compact["models"][0]["multi_agent_version"] = "v2"

        model = render_full_catalog(compact)["models"][0]

        self.assertEqual(model["tool_mode"], "code_mode_only")
        self.assertIs(model["include_plugin_usage_instructions"], False)
        self.assertEqual(model["multi_agent_version"], "v2")
        self.assertTrue(model["comp_hash"])
        self.assertEqual(model["default_service_tier"], None)

    def test_model_messages_passthrough_keys_are_never_dropped(self) -> None:
        compact = compact_catalog()
        compact["models"][0]["model_messages"] = {
            "approvals": {"some": "policy"},
            "token_budget": {"reminder_threshold_tokens": 6144},
            "permissions": {"allow": ["read"]},
        }

        messages = render_full_catalog(compact)["models"][0]["model_messages"]

        self.assertEqual(messages["approvals"], {"some": "policy"})
        self.assertEqual(messages["token_budget"], {"reminder_threshold_tokens": 6144})
        self.assertEqual(messages["permissions"], {"allow": ["read"]})
        self.assertIsNone(messages["auto_review"])
        self.assertIsNone(messages["collaboration_modes"])
        self.assertIsNone(messages["instructions_variables"])


if __name__ == "__main__":
    unittest.main()
