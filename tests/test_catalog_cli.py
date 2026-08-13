import json
import os
import tempfile
import unittest
import unittest.mock

from opencode_go_proxy import catalog, protocol


class CatalogCliTest(unittest.TestCase):
    def test_main_refresh_writes_catalog(self):
        tmp = tempfile.mkdtemp()
        tmp_compact = os.path.join(tmp, "compact.json")
        tmp_cat = os.path.join(tmp, "catalog.json")
        compact = {
            "fetched_at": "2026-08-10T00:00:00+00:00",
            "etag": "w/1",
            "client_version": "0.147.0",
            "shared_instructions": "You are Codex.",
            "models": [
                {
                    "slug": "deepseek-v4-flash",
                    "display_name": "DeepSeek V4 Flash",
                    "description": "d",
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": ["low", "medium", "high"],
                    "context_window": 1000000,
                    "max_context_window": 1000000,
                }
            ],
        }
        with open(tmp_compact, "w") as f:
            json.dump(compact, f)
        with unittest.mock.patch.dict(os.environ, {"OPENCODE_GO_CATALOG_REFRESH": "0"}):
            rc = catalog.main_refresh(["--compact", tmp_compact, "--catalog", tmp_cat])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(tmp_cat))
        with open(tmp_cat) as f:
            rendered = json.load(f)
        self.assertEqual(len(rendered["models"]), 1)
        self.assertIn("base_instructions", rendered["models"][0])

    def test_known_models_loads_from_catalog_env(self):
        tmp = tempfile.mkdtemp()
        tmp_cat = os.path.join(tmp, "catalog.json")
        compact = {
            "fetched_at": "2026-08-10T00:00:00+00:00",
            "etag": "w/1",
            "client_version": "0.147.0",
            "shared_instructions": "You are Codex.",
            "models": [
                {
                    "slug": "deepseek-v4-flash",
                    "display_name": "DeepSeek V4 Flash",
                    "description": "d",
                    "default_reasoning_level": "medium",
                    "supported_reasoning_levels": ["low", "medium", "high"],
                    "context_window": 1000000,
                    "max_context_window": 1000000,
                }
            ],
        }
        catalog.write_catalog(compact, tmp_cat)
        old = os.environ.get("CODEX_MODEL_CATALOG")
        try:
            os.environ["CODEX_MODEL_CATALOG"] = tmp_cat
            self.assertIn("deepseek-v4-flash", protocol.reload_known_models())
        finally:
            if old is None:
                os.environ.pop("CODEX_MODEL_CATALOG", None)
            else:
                os.environ["CODEX_MODEL_CATALOG"] = old
            protocol.reload_known_models()


if __name__ == "__main__":
    unittest.main()
