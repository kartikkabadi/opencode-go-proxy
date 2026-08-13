"""Hermetic test environment: isolated state dir + seeded catalog.

HTTP handlers record usage-meter events on every turn and model routing reads
the catalog; without this fixture tests write into the real state dir and
depend on the real machine catalog file. Autouse so no test can forget.
"""
import json
import os

import pytest


@pytest.fixture(autouse=True)
def isolated_test_env(tmp_path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "deepseek-v4-flash", "display_name": "DeepSeek V4 Flash"},
                    {"slug": "deepseek-v4-pro", "display_name": "DeepSeek V4 Pro"},
                ]
            }
        )
    )
    old = {
        "CODEX_MODEL_CATALOG": os.environ.get("CODEX_MODEL_CATALOG"),
        "OPENCODE_GO_PROXY_STATE_DIR": os.environ.get("OPENCODE_GO_PROXY_STATE_DIR"),
    }
    os.environ["CODEX_MODEL_CATALOG"] = str(catalog)
    os.environ["OPENCODE_GO_PROXY_STATE_DIR"] = str(tmp_path / "state")
    yield
    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
