"""Agent TOML sync: writes one TOML per opencode-go model, removes stale owned
files, and leaves files owned by other tools alone.

The state dir is isolated by conftest; the agents dir is redirected via
OPENCODE_GO_PROXY_AGENTS_DIR.
"""

import json
import os
import tomllib

import pytest

from opencode_go_proxy import agents_sync, catalog


def _write_compact(state_dir: str, slugs: list[str]) -> None:
    compact = {
        "fetched_at": "2026-08-10T00:00:00Z",
        "etag": "",
        "shared_instructions": "",
        "client_version": "0.147.0",
        "models": [
            {"slug": slug, "display_name": slug.replace("-", " ").title()}
            for slug in slugs
        ],
    }
    path = os.path.join(state_dir, catalog.STATE_COMPACT_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(compact, handle)


@pytest.fixture
def agents_dir(tmp_path, monkeypatch) -> str:
    path = str(tmp_path / "agents")
    monkeypatch.setenv("OPENCODE_GO_PROXY_AGENTS_DIR", path)
    return path


def _state_dir() -> str:
    return os.environ["OPENCODE_GO_PROXY_STATE_DIR"]


class TestSyncAgents:
    def test_writes_one_toml_per_model(self, agents_dir) -> None:
        _write_compact(_state_dir(), ["deepseek-v4-flash", "deepseek-v4-pro"])
        report = agents_sync.sync_agents()
        assert report["catalog"] == "ok"
        assert report["written"] == [
            "router-opencode-go-deepseek-v4-flash.toml",
            "router-opencode-go-deepseek-v4-pro.toml",
        ]
        first = os.path.join(agents_dir, "router-opencode-go-deepseek-v4-flash.toml")
        with open(first, "rb") as handle:
            parsed = tomllib.load(handle)
        assert parsed["name"] == "router_opencode_go_deepseek-v4-flash"
        assert parsed["model_provider"] == "opencode-go"
        assert parsed["model"] == "opencode-go/deepseek-v4-flash"
        with open(first, encoding="utf-8") as handle:
            text = handle.read()
        assert agents_sync.OWNERSHIP_MARKER in text

    def test_second_sync_is_idempotent(self, agents_dir) -> None:
        _write_compact(_state_dir(), ["deepseek-v4-flash"])
        agents_sync.sync_agents()
        report = agents_sync.sync_agents()
        assert report["written"] == []
        assert report["unchanged"] == ["router-opencode-go-deepseek-v4-flash.toml"]
        assert report["removed"] == []

    def test_removes_stale_owned(self, agents_dir) -> None:
        _write_compact(_state_dir(), ["deepseek-v4-flash", "deepseek-v4-pro"])
        agents_sync.sync_agents()
        _write_compact(_state_dir(), ["deepseek-v4-flash"])
        report = agents_sync.sync_agents()
        assert report["removed"] == ["router-opencode-go-deepseek-v4-pro.toml"]
        assert not os.path.exists(os.path.join(agents_dir, "router-opencode-go-deepseek-v4-pro.toml"))
        assert os.path.exists(os.path.join(agents_dir, "router-opencode-go-deepseek-v4-flash.toml"))

    def test_leaves_foreign_files_alone(self, agents_dir) -> None:
        _write_compact(_state_dir(), ["deepseek-v4-flash"])
        foreign = os.path.join(agents_dir, "router-model-opencode-go-glm-5-1.toml")
        os.makedirs(agents_dir, exist_ok=True)
        with open(foreign, "w", encoding="utf-8") as handle:
            handle.write("# Managed by Codex Router.\nname = \"router_opencode_go_glm_5_1\"\n")
        report = agents_sync.sync_agents()
        assert os.path.basename(foreign) in os.listdir(agents_dir)
        assert report["removed"] == []

    def test_noop_when_catalog_missing(self, agents_dir, monkeypatch) -> None:
        stale = os.path.join(agents_dir, "router-opencode-go-deepseek-v4-pro.toml")
        os.makedirs(agents_dir, exist_ok=True)
        with open(stale, "w", encoding="utf-8") as handle:
            handle.write(f"{agents_sync.OWNERSHIP_MARKER}\nname = \"router_opencode_go_deepseek_v4_pro\"\n")
        monkeypatch.setattr(agents_sync.catalog, "load_runtime_compact", lambda: None)
        monkeypatch.setattr(agents_sync.catalog, "load_seed_compact", lambda: None)
        report = agents_sync.sync_agents()
        assert report["catalog"] == "missing"
        assert os.path.exists(stale)


class TestCli:
    def test_agents_sync_cmd(self, agents_dir, capsys) -> None:
        _write_compact(_state_dir(), ["deepseek-v4-flash"])
        assert agents_sync.agents_sync_cmd([]) == 0
        out = capsys.readouterr().out
        assert "1 written" in out
        assert "router-opencode-go-deepseek-v4-flash.toml" in out

    def test_agents_sync_cmd_json(self, agents_dir, capsys) -> None:
        _write_compact(_state_dir(), ["deepseek-v4-flash"])
        assert agents_sync.agents_sync_cmd(["--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["written"] == ["router-opencode-go-deepseek-v4-flash.toml"]
