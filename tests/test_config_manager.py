"""Config manager: marker-block enable/disable/status on a temp config.toml.

Every test targets OPENCODE_GO_PROXY_CONFIG_PATH in tmp_path; the real
~/.codex/config.toml is never read or written.
"""

import json
import os
from unittest import mock

import pytest

from opencode_go_proxy import config_manager


@pytest.fixture
def cfg_path(tmp_path, monkeypatch) -> str:
    path = str(tmp_path / "config.toml")
    monkeypatch.setenv("OPENCODE_GO_PROXY_CONFIG_PATH", path)
    return path


@pytest.fixture(autouse=True)
def fake_codex_bin(tmp_path, monkeypatch) -> str:
    """Point OPENCODE_GO_PROXY_CODEX_BIN at a script that accepts multi_agent_v2."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n")
    codex.chmod(0o755)
    monkeypatch.setenv("OPENCODE_GO_PROXY_CODEX_BIN", str(codex))
    return str(codex)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestEnable:
    def test_writes_block_and_voice_keys(self, cfg_path) -> None:
        report = config_manager.enable()
        assert report["changed"] is True
        text = _read(cfg_path)
        assert config_manager.START_MARKER in text
        assert config_manager.END_MARKER in text
        assert f"openai_base_url = {json.dumps(config_manager.managed_base_url())}" in text
        assert f"model_catalog_json = {json.dumps(config_manager.managed_catalog_path())}" in text
        assert config_manager.REALTIME_CALL_KEY in text
        assert config_manager.REALTIME_WS_KEY in text
        assert text.strip().endswith(config_manager.END_MARKER)

    def test_writes_multi_agent_v2_and_provider_block(self, cfg_path) -> None:
        report = config_manager.enable()
        assert report["multi_agent_v2"] is True
        text = _read(cfg_path)
        assert "[features.multi_agent_v2]" in text
        assert "enabled = true" in text
        assert "[model_providers.opencode-go]" in text
        assert 'name = "opencode-go"' in text
        assert f"base_url = {json.dumps(config_manager.managed_base_url())}" in text
        assert 'wire_api = "responses"' in text

    def test_omits_feature_block_when_probe_fails(self, cfg_path, tmp_path, monkeypatch) -> None:
        bin_dir = tmp_path / "bin-fail"
        bin_dir.mkdir()
        codex = bin_dir / "codex"
        codex.write_text("#!/bin/sh\nexit 1\n")
        codex.chmod(0o755)
        monkeypatch.setenv("OPENCODE_GO_PROXY_CODEX_BIN", str(codex))
        report = config_manager.enable()
        assert report["multi_agent_v2"] is False
        text = _read(cfg_path)
        assert "[features.multi_agent_v2]" not in text
        assert "[model_providers.opencode-go]" in text

    def test_skips_feature_block_when_user_configures_it(self, cfg_path) -> None:
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write("multi_agent_v2 = { enabled = true }\n")
        config_manager.enable()
        text = _read(cfg_path)
        assert "[features.multi_agent_v2]" not in text

    def test_omits_provider_keys_user_owns(self, cfg_path) -> None:
        user = '[model_providers.opencode-go]\nbase_url = "https://user.example/v1"\nwire_api = "responses"\n'
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write(user)
        config_manager.enable()
        text = _read(cfg_path)
        assert text.count("[model_providers.opencode-go]") == 1
        provider_section = text.split("[model_providers.opencode-go]", 1)[1]
        assert 'base_url = "https://user.example/v1"' in provider_section
        assert "8787" not in provider_section

    def test_creates_parent_dir_and_mode_600(self, tmp_path, monkeypatch) -> None:
        nested = tmp_path / "codex" / "config.toml"
        monkeypatch.setenv("OPENCODE_GO_PROXY_CONFIG_PATH", str(nested))
        config_manager.enable()
        assert (nested.stat().st_mode & 0o777) == 0o600

    def test_preserves_user_root_and_tables(self, cfg_path) -> None:
        user = (
            'model = "gpt-5.6-luna"\n'
            "model_provider = \"openai\"\n"
            '\n'
            '[model_providers.opencode-go]\n'
            'name = "Go"\n'
        )
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write(user)
        config_manager.enable()
        text = _read(cfg_path)
        assert text.startswith('model = "gpt-5.6-luna"\n')
        assert 'model_provider = "openai"' in text
        assert '[model_providers.opencode-go]\nname = "Go"' in text
        assert text.index(config_manager.START_MARKER) < text.index("[model_providers")

    def test_refuses_user_owned_base_url(self, cfg_path) -> None:
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write('openai_base_url = "https://api.openai.com/v1"\n')
        before = _read(cfg_path)
        with pytest.raises(config_manager.ConfigError, match="refusing to replace user-owned openai_base_url"):
            config_manager.enable()
        assert _read(cfg_path) == before

    def test_refuses_user_owned_catalog(self, cfg_path) -> None:
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write('model_catalog_json = "/home/you/.codex/model-catalogs/custom.json"\n')
        before = _read(cfg_path)
        with pytest.raises(config_manager.ConfigError, match="refusing to replace user-owned model_catalog_json"):
            config_manager.enable()
        assert _read(cfg_path) == before

    def test_adopts_matching_user_values_and_is_idempotent(self, cfg_path) -> None:
        base = config_manager.managed_base_url()
        catalog = config_manager.managed_catalog_path()
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write(f'openai_base_url = {json.dumps(base)}\nmodel_catalog_json = {json.dumps(catalog)}\n')
        assert config_manager.enable()["changed"] is True
        assert config_manager.enable()["changed"] is False
        assert _read(cfg_path).count(config_manager.START_MARKER) == 1

    def test_keeps_user_voice_keys_untouched(self, cfg_path) -> None:
        user_value = "https://user.example.com/webrtc"
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write(f"{config_manager.REALTIME_CALL_KEY} = {json.dumps(user_value)}\n")
        config_manager.enable()
        text = _read(cfg_path)
        assert f"{config_manager.REALTIME_CALL_KEY} = {json.dumps(user_value)}" in text
        assert text.count(config_manager.REALTIME_CALL_KEY) == 1
        assert config_manager.REALTIME_WS_KEY in text

    def test_voice_call_derives_from_chatgpt_base_url(self, cfg_path, monkeypatch) -> None:
        monkeypatch.setenv("OPENCODE_GO_PROXY_MANAGED_BASE_URL", "http://127.0.0.1:8790/v1")
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write('chatgpt_base_url = "https://custom.example.com/backend-api"\n')
        config_manager.enable()
        text = _read(cfg_path)
        assert f'{config_manager.REALTIME_CALL_KEY} = "https://custom.example.com/backend-api/codex"' in text

    def test_refuses_multiple_blocks(self, cfg_path) -> None:
        block = f"{config_manager.START_MARKER}\n# x\n{config_manager.END_MARKER}\n"
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write(block + block)
        before = _read(cfg_path)
        with pytest.raises(config_manager.ConfigError, match="refusing to guess"):
            config_manager.enable()
        assert _read(cfg_path) == before

    def test_leaves_foreign_managed_blocks_alone(self, cfg_path) -> None:
        foreign = "# BEGIN codex-router-managed\nopenai_base_url = \"http://127.0.0.1:1234/v1\"\n# END codex-router-managed\n"
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write(foreign)
        with pytest.raises(config_manager.ConfigError, match="user-owned openai_base_url"):
            config_manager.enable()
        assert _read(cfg_path) == foreign


class TestDisable:
    def test_round_trip_restores_user_content(self, cfg_path) -> None:
        user = '[model_providers.opencode-go]\nname = "Go"\n'
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write(user)
        config_manager.enable()
        assert config_manager.disable()["changed"] is True
        text = _read(cfg_path)
        assert config_manager.START_MARKER not in text
        assert text == user

    def test_removes_file_when_block_was_only_content(self, cfg_path) -> None:
        config_manager.enable()
        report = config_manager.disable()
        assert report["file_removed"] is True
        assert not os.path.exists(cfg_path)

    def test_noop_when_disabled(self, cfg_path) -> None:
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write('[profiles.x]\nmodel = "a"\n')
        report = config_manager.disable()
        assert report["changed"] is False
        assert _read(cfg_path) == '[profiles.x]\nmodel = "a"\n'

    def test_missing_file_noop(self, cfg_path) -> None:
        assert config_manager.disable()["changed"] is False

    def test_keeps_user_voice_keys_outside_block(self, cfg_path) -> None:
        user_voice = f"{config_manager.REALTIME_WS_KEY} = \"https://user.example.com/ws\"\n"
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write(user_voice)
        config_manager.enable()
        config_manager.disable()
        assert _read(cfg_path) == user_voice


class TestStatus:
    def test_disabled_when_absent(self, cfg_path) -> None:
        report = config_manager.status()
        assert report["state"] == "disabled"
        assert report["managed"] is False
        assert report["exists"] is False

    def test_enabled_after_enable(self, cfg_path) -> None:
        config_manager.enable()
        report = config_manager.status()
        assert report["state"] == "enabled"
        assert report["managed"] is True
        assert report["openai_base_url"] == config_manager.managed_base_url()
        assert report["model_catalog_json"] == config_manager.managed_catalog_path()
        assert report["multi_agent_v2"] is True
        assert report["provider_block"] is True
        assert config_manager.REALTIME_CALL_KEY in report["voice_keys_managed"]
        assert report["voice_keys_user_owned"] == []

    def test_reports_user_voice_keys(self, cfg_path) -> None:
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write(f'{config_manager.REALTIME_CALL_KEY} = "https://user.example.com/webrtc"\n')
        config_manager.enable()
        report = config_manager.status()
        assert report["voice_keys_user_owned"] == [config_manager.REALTIME_CALL_KEY]
        assert report["voice_keys_managed"] == [config_manager.REALTIME_WS_KEY]


class TestCli:
    def test_status_json(self, cfg_path, capsys) -> None:
        assert config_manager.config_cmd(["status", "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["state"] == "disabled"

    def test_enable_disable_round_trip(self, cfg_path, capsys) -> None:
        assert config_manager.config_cmd(["enable"]) == 0
        assert "managed block written" in capsys.readouterr().out
        assert config_manager.config_cmd(["status"]) == 0
        assert "state: enabled" in capsys.readouterr().out
        assert config_manager.config_cmd(["disable"]) == 0
        assert "file removed" in capsys.readouterr().out
        assert not os.path.exists(cfg_path)

    def test_usage_error(self, capsys) -> None:
        assert config_manager.config_cmd([]) == 2
        assert config_manager.config_cmd(["bogus"]) == 2

    def test_refusal_exit_code(self, cfg_path, capsys) -> None:
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write('openai_base_url = "https://api.openai.com/v1"\n')
        assert config_manager.config_cmd(["enable"]) == 1
        assert "refusing to replace" in capsys.readouterr().err


class TestZenProviderBlock:
    def test_zen_block_rendered_next_to_opencode_go(self, cfg_path) -> None:
        config_manager.enable()
        text = _read(cfg_path)
        assert "[model_providers.opencode-go]" in text
        assert "[model_providers.zen]" in text
        assert 'name = "zen"' in text
        assert f"base_url = {json.dumps(config_manager.managed_base_url())}" in text
        assert 'wire_api = "responses"' in text
        assert text.index("[model_providers.opencode-go]") > text.index(config_manager.START_MARKER)
        assert text.index("[model_providers.zen]") > text.index(config_manager.START_MARKER)
        assert text.index(config_manager.END_MARKER) > text.index("[model_providers.zen]")

    def test_omits_zen_block_when_user_owns_zen_keys(self, cfg_path) -> None:
        user = '[model_providers.zen]\nbase_url = "https://user.example/v1"\nwire_api = "responses"\n'
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write(user)
        config_manager.enable()
        text = _read(cfg_path)
        assert text.count("[model_providers.zen]") == 1
        zen_section = text.split("[model_providers.zen]", 1)[1]
        assert 'base_url = "https://user.example/v1"' in zen_section
        assert "8787" not in zen_section
        assert "[model_providers.opencode-go]" in text

    def test_enable_disable_round_trip_removes_both_blocks(self, cfg_path) -> None:
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write('model = "gpt-5.6-luna"\n')
        config_manager.enable()
        text = _read(cfg_path)
        assert "[model_providers.opencode-go]" in text
        assert "[model_providers.zen]" in text
        config_manager.disable()
        text = _read(cfg_path)
        assert "[model_providers.opencode-go]" not in text
        assert "[model_providers.zen]" not in text
        assert text == 'model = "gpt-5.6-luna"\n'

    def test_status_reports_zen_block(self, cfg_path) -> None:
        config_manager.enable()
        report = config_manager.status()
        assert report["provider_block"] is True
        assert report["zen_provider_block"] is True
        config_manager.disable()
        report = config_manager.status()
        assert report["zen_provider_block"] is False


class TestNoDuplicateKeys:
    def test_enable_with_matching_user_values_has_no_duplicate_keys(self, tmp_path, fake_codex_bin) -> None:
        path = tmp_path / "config.toml"
        state = tmp_path / "state"
        with mock.patch.dict(
            os.environ,
            {
                "OPENCODE_GO_PROXY_CONFIG_PATH": str(path),
                "OPENCODE_GO_PROXY_STATE_DIR": str(state),
                "OPENCODE_GO_PROXY_CODEX_BIN": fake_codex_bin,
            },
            clear=True,
        ):
            base_url = config_manager.managed_base_url()
            catalog_path = config_manager.managed_catalog_path()
            path.write_text(
                f'openai_base_url = "{base_url}"\n'
                f'model_catalog_json = "{catalog_path}"\n'
            )
            result = config_manager.enable()
        assert result["changed"] is True
        text = path.read_text()
        assert text.count("openai_base_url") == 1
        assert text.count("model_catalog_json") == 1
        assert config_manager.START_MARKER in text


def _backup_names(directory: str, cfg_path: str) -> list[str]:
    prefix = os.path.basename(cfg_path) + ".bak-"
    return sorted(name for name in os.listdir(directory) if name.startswith(prefix))


class TestBackup:
    def test_second_enable_without_change_writes_no_second_backup(self, cfg_path) -> None:
        """A no-op enable() (already enabled) must not write another backup."""
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write('model = "gpt-5.6-luna"\n')
        config_manager.enable()
        directory = os.path.dirname(cfg_path)
        backups = _backup_names(directory, cfg_path)
        assert len(backups) == 1
        assert config_manager.enable()["changed"] is False
        assert _backup_names(directory, cfg_path) == backups

    def test_disable_writes_backup_before_removing_block(self, cfg_path) -> None:
        """disable() snapshots the managed config before rewriting it."""
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write('model = "gpt-5.6-luna"\n')
        config_manager.enable()
        enabled_text = _read(cfg_path)
        assert config_manager.disable()["changed"] is True
        directory = os.path.dirname(cfg_path)
        backups = _backup_names(directory, cfg_path)
        assert len(backups) == 2
        assert _read(os.path.join(directory, backups[-1])) == enabled_text

    def test_disable_file_removal_still_backs_up(self, cfg_path) -> None:
        """Removing the file (block was its only content) still snapshots it."""
        config_manager.enable()  # file was absent, so enable writes no backup
        enabled_text = _read(cfg_path)
        assert config_manager.disable()["file_removed"] is True
        directory = os.path.dirname(cfg_path)
        backups = _backup_names(directory, cfg_path)
        assert len(backups) == 1
        assert _read(os.path.join(directory, backups[-1])) == enabled_text
