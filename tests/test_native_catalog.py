"""Native model capture and merged-catalog rendering."""

import json
import os
from unittest import mock

from opencode_go_proxy import catalog
from opencode_go_proxy import native_models
from opencode_go_proxy.meter import state_dir

NATIVE_FIXTURE = {
    "models": [
        {
            "slug": "gpt-5.6-luna",
            "display_name": "GPT-5.6-Luna",
            "description": "Luna.",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [
                {"effort": "low", "description": "fast"},
                {"effort": "medium", "description": "balanced"},
                {"effort": "max", "description": "max"},
            ],
            "multi_agent_version": "v1",
            "context_window": 272000,
            "shell_type": "shell_command",
            "visibility": "list",
        },
        {
            "slug": "gpt-5.6-terra",
            "display_name": "GPT-5.6-Terra",
            "description": "Terra.",
            "default_reasoning_level": "high",
            "supported_reasoning_levels": [
                {"effort": "high", "description": "deep"},
                {"effort": "ultra", "description": "ultra"},
            ],
            "multi_agent_version": "v2",
            "context_window": 272000,
            "shell_type": "shell_command",
            "visibility": "list",
        },
    ]
}


def _write_fake_codex_bin(bin_path) -> None:
    script = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if sys.argv[1] == '--version':\n"
        "    print('codex-cli 0.146.0-test')\n"
        "else:\n"
        "    print('" + json.dumps(NATIVE_FIXTURE) + "')\n"
    )
    bin_path.write_text(script)
    bin_path.chmod(0o755)


def _seed_native_capture(models: list[dict] | None = None) -> None:
    state = state_dir()
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, native_models.NATIVE_CATALOG_NAME), "w") as handle:
        json.dump(
            {
                "captured_at": "2026-08-13T00:00:00Z",
                "captured_with": "codex-cli 0.146.0-test",
                "models": models if models is not None else NATIVE_FIXTURE["models"],
            },
            handle,
        )


def test_capture_runs_fake_codex_and_writes_snapshot(tmp_path) -> None:
    bin_path = tmp_path / "codex"
    _write_fake_codex_bin(bin_path)
    with mock.patch.dict(os.environ, {native_models.CODEX_BIN_ENV: str(bin_path)}):
        snapshot = native_models.capture_native_models()
    assert [m["slug"] for m in snapshot["models"]] == ["gpt-5.6-luna", "gpt-5.6-terra"]
    assert snapshot["captured_with"] == "codex-cli 0.146.0-test"
    assert snapshot["captured_at"]

    saved = json.load(open(native_models.native_models_path()))
    luna = saved["models"][0]
    assert luna["slug"] == "gpt-5.6-luna"
    assert luna["multi_agent_version"] == "v1"
    assert luna["context_window"] == 272000
    # Only the kept keys are projected into the snapshot.
    assert "shell_type" not in luna
    assert "visibility" not in luna


def test_capture_excludes_prefixed_and_opencode_go_slugs(tmp_path) -> None:
    # codex debug models renders the configured catalog: prefixed
    # opencode-go entries and bare opencode-go slugs (once the merged
    # catalog is configured) must never become "native".
    polluted = {
        "models": [
            {"slug": "gpt-5.6-luna", "display_name": "Luna"},
            {"slug": "opencode-go/deepseek-v4-flash", "display_name": "Prefixed"},
            {"slug": "opencode-go-messages/qwen3.7-max", "display_name": "Prefixed2"},
            {"slug": "deepseek-v4-flash", "display_name": "Bare opencode-go"},
        ]
    }
    bin_path = tmp_path / "codex"
    script = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if sys.argv[1] == '--version':\n"
        "    print('codex-cli 0.146.0-test')\n"
        "else:\n"
        "    print('" + json.dumps(polluted) + "')\n"
    )
    bin_path.write_text(script)
    bin_path.chmod(0o755)
    with mock.patch.dict(os.environ, {native_models.CODEX_BIN_ENV: str(bin_path)}):
        snapshot = native_models.capture_native_models()
    assert {m["slug"] for m in snapshot["models"]} == {"gpt-5.6-luna"}


def test_dry_run_prints_and_does_not_write(tmp_path, capsys) -> None:
    bin_path = tmp_path / "codex"
    _write_fake_codex_bin(bin_path)
    with mock.patch.dict(os.environ, {native_models.CODEX_BIN_ENV: str(bin_path)}):
        rc = native_models.native_capture_cmd(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 native models" in out
    assert "max" in out and "ultra" in out
    assert not os.path.exists(native_models.native_models_path())


def test_capture_writes_and_prints_target(tmp_path, capsys) -> None:
    bin_path = tmp_path / "codex"
    _write_fake_codex_bin(bin_path)
    with mock.patch.dict(os.environ, {native_models.CODEX_BIN_ENV: str(bin_path)}):
        rc = native_models.native_capture_cmd([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "captured 2 native models" in out
    assert os.path.exists(native_models.native_models_path())


def test_missing_bin_errors_cleanly(capsys) -> None:
    with mock.patch.dict(
        os.environ, {native_models.CODEX_BIN_ENV: "/nonexistent/codex-bin"}
    ):
        rc = native_models.native_capture_cmd(["--dry-run"])
    assert rc == 1
    assert "error" in capsys.readouterr().err


def test_missing_capture_loads_empty() -> None:
    capture = native_models.load_native_capture()
    assert capture["models"] == []
    assert native_models.native_slugs(capture) == set()
    assert native_models.native_effort_vocabulary(capture) == set()


def test_merged_catalog_contains_native_and_opencode_go_entries() -> None:
    _seed_native_capture()
    merged = catalog.render_merged_catalog()
    slugs = {m["slug"] for m in merged["models"]}
    assert "gpt-5.6-luna" in slugs  # native entry, captured slug
    assert "gpt-5.6-terra" in slugs
    assert "deepseek-v4-flash" in slugs  # opencode-go entry, bare slug
    luna = next(m for m in merged["models"] if m["slug"] == "gpt-5.6-luna")
    assert luna["multi_agent_version"] == "v1"
    assert luna["context_window"] == 272000
    # Native entries are rendered into the full canonical shape.
    assert luna["model_messages"]
    assert luna["auto_compact_token_limit"] > 0
    assert os.path.exists(catalog.merged_models_path())


def test_merged_catalog_without_capture_is_opencode_go_only() -> None:
    merged = catalog.render_merged_catalog()
    slugs = {m["slug"] for m in merged["models"]}
    assert "gpt-5.6-luna" not in slugs
    assert "deepseek-v4-flash" in slugs


def test_effort_clamp_drops_unknown_efforts() -> None:
    state = state_dir()
    os.makedirs(state, exist_ok=True)
    compact = {
        "fetched_at": "2026-08-13T00:00:00Z",
        "etag": "test",
        "shared_instructions": "",
        "client_version": "0.147.0",
        "models": [
            {
                "slug": "clamp-me",
                "display_name": "Clamp Me",
                "context_window": 100000,
                "supported_reasoning_levels": [
                    {"effort": "low", "description": "ok"},
                    {"effort": "medium", "description": "ok"},
                    {"effort": "ultra-super", "description": "not native"},
                ],
            }
        ],
    }
    with open(os.path.join(state, catalog.STATE_COMPACT_NAME), "w") as handle:
        json.dump(compact, handle)
    _seed_native_capture()  # effort vocabulary: low, medium, max, high, ultra
    merged = catalog.render_merged_catalog()
    clamped = next(m for m in merged["models"] if m["slug"] == "clamp-me")
    efforts = [level["effort"] for level in clamped["supported_reasoning_levels"]]
    assert efforts == ["low", "medium"]


def test_effort_clamp_skipped_without_capture() -> None:
    state = state_dir()
    os.makedirs(state, exist_ok=True)
    compact = {
        "fetched_at": "2026-08-13T00:00:00Z",
        "etag": "test",
        "shared_instructions": "",
        "client_version": "0.147.0",
        "models": [
            {
                "slug": "clamp-me",
                "display_name": "Clamp Me",
                "supported_reasoning_levels": [{"effort": "ultra-super", "description": "x"}],
            }
        ],
    }
    with open(os.path.join(state, catalog.STATE_COMPACT_NAME), "w") as handle:
        json.dump(compact, handle)
    merged = catalog.render_merged_catalog()
    entry = next(m for m in merged["models"] if m["slug"] == "clamp-me")
    assert [level["effort"] for level in entry["supported_reasoning_levels"]] == ["ultra-super"]
