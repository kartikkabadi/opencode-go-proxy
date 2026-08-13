"""Config-manager edge matrix: enable/disable/status across key ownership.

Extends tests/test_config_manager.py with the four provider-key ownership
combinations (no user keys, user owns the opencode-go key, user owns the zen
key, user owns both), the idempotency guarantees, TOML validity, marker
boundaries, provider-table preservation, and the backup-before-edit gate.

Every test is fully hermetic: the config path, state dir (catalog render),
and codex binary all live under tmp_path; the real ~/.codex/config.toml and
the real codex binary are never touched.
"""

import json
import os
import tomllib

import pytest

from opencode_go_proxy import config_manager

# A user-declared provider table "owns" a key: the managed block must omit the
# table entirely (TOML forbids splitting one table across two locations) and
# must leave the user's fields byte-for-byte intact.
USER_OP = (
    "[model_providers.opencode-go]\n"
    'name = "OpenCode Go (user)"\n'
    'base_url = "https://user.example/v1"\n'
    'env_key = "OPENCODE_GO_API_KEY"\n'
    'wire_api = "responses"\n'
)
USER_ZEN = (
    "[model_providers.zen]\n"
    'name = "Zen (user)"\n'
    'base_url = "https://user.example/zen/v1"\n'
    'env_key = "OPENCODE_ZEN_API_KEY"\n'
    'wire_api = "responses"\n'
)


@pytest.fixture
def cfg_path(tmp_path, monkeypatch) -> str:
    """Temp config.toml plus an isolated state dir for the catalog render."""
    path = str(tmp_path / "config.toml")
    monkeypatch.setenv("OPENCODE_GO_PROXY_CONFIG_PATH", path)
    monkeypatch.setenv("OPENCODE_GO_PROXY_STATE_DIR", str(tmp_path / "state"))
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


def _block_text(text: str) -> str:
    """The managed marker block, START marker through END marker inclusive."""
    start = text.index(config_manager.START_MARKER)
    end = text.index(config_manager.END_MARKER)
    return text[start : end + len(config_manager.END_MARKER)]


def _write(path: str, contents: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(contents)


def _assert_managed_block(block: str) -> None:
    """Marker boundaries: exactly one BEGIN/END pair, END last in the block."""
    assert block.startswith(config_manager.START_MARKER)
    assert block.rstrip().endswith(config_manager.END_MARKER)
    assert block.count(config_manager.START_MARKER) == 1
    assert block.count(config_manager.END_MARKER) == 1


def _assert_provider_table(block: str, name: str, *, present: bool) -> None:
    """A managed provider table renders name/base_url/wire_api exactly once."""
    header = f"[model_providers.{name}]"
    if present:
        assert header in block
        table = block.split(header, 1)[1]
        assert f'name = "{name}"' in table
        assert f"base_url = {json.dumps(config_manager.managed_base_url())}" in table
        assert 'wire_api = "responses"' in table
    else:
        assert header not in block


@pytest.mark.parametrize(
    ("user_config", "managed_op", "managed_zen"),
    [
        pytest.param("", True, True, id="no-user-keys"),
        pytest.param(USER_OP, False, True, id="user-owns-opencode-go"),
        pytest.param(USER_ZEN, True, False, id="user-owns-zen"),
        pytest.param(USER_OP + "\n" + USER_ZEN, False, False, id="user-owns-both"),
    ],
)
class TestEnableMatrix:
    def test_enable_renders_valid_toml_with_correct_blocks(
        self, cfg_path, user_config: str, managed_op: bool, managed_zen: bool
    ) -> None:
        if user_config:
            _write(cfg_path, user_config)
        report = config_manager.enable()
        assert report["changed"] is True
        text = _read(cfg_path)
        data = tomllib.loads(text)
        block = _block_text(text)

        _assert_managed_block(block)
        _assert_provider_table(block, "opencode-go", present=managed_op)
        _assert_provider_table(block, "zen", present=managed_zen)

        # Root wiring the managed block owns.
        assert data["openai_base_url"] == config_manager.managed_base_url()
        assert data["model_catalog_json"] == config_manager.managed_catalog_path()
        assert (
            f"model_catalog_json = {json.dumps(config_manager.managed_catalog_path())}" in block
        )
        assert config_manager.REALTIME_CALL_KEY in block
        assert config_manager.REALTIME_WS_KEY in block
        assert data[config_manager.REALTIME_WS_KEY] == config_manager.DEFAULT_REALTIME_WS_BASE_URL

        # multi_agent_v2 probe passes and the user does not configure it: the
        # managed feature block is present and parses.
        assert "[features.multi_agent_v2]" in block
        assert data["features"]["multi_agent_v2"]["enabled"] is True

        # Whatever the ownership state, exactly one table per provider parses.
        providers = data["model_providers"]
        assert set(providers) == {"opencode-go", "zen"}
        assert text.count("[model_providers.opencode-go]") == 1
        assert text.count("[model_providers.zen]") == 1

    def test_enable_twice_is_byte_idempotent(
        self, cfg_path, user_config: str, managed_op: bool, managed_zen: bool
    ) -> None:
        del managed_op, managed_zen
        if user_config:
            _write(cfg_path, user_config)
        config_manager.enable()
        first = _read(cfg_path)
        report = config_manager.enable()
        assert report["changed"] is False
        assert _read(cfg_path) == first
        assert _read(cfg_path).count(config_manager.START_MARKER) == 1
        assert _read(cfg_path).count(config_manager.END_MARKER) == 1

    def test_status_reports_provider_blocks_per_ownership(
        self, cfg_path, user_config: str, managed_op: bool, managed_zen: bool
    ) -> None:
        if user_config:
            _write(cfg_path, user_config)
        config_manager.enable()
        report = config_manager.status()
        assert report["state"] == "enabled"
        assert report["managed"] is True
        assert report["provider_block"] is managed_op
        assert report["zen_provider_block"] is managed_zen
        assert set(report["voice_keys_managed"]) == {
            config_manager.REALTIME_CALL_KEY,
            config_manager.REALTIME_WS_KEY,
        }
        assert report["voice_keys_user_owned"] == []
        config_manager.disable()
        after = config_manager.status()
        assert after["state"] == "disabled"
        assert after["provider_block"] is False
        assert after["zen_provider_block"] is False


class TestDisableEdge:
    def test_disable_removes_both_blocks_and_keeps_user_content(self, cfg_path) -> None:
        user = (
            'model = "gpt-5.6-luna"\n'
            'model_provider = "opencode-go"\n'
            "\n"
            + USER_OP
            + "\n"
            + USER_ZEN
        )
        _write(cfg_path, user)
        config_manager.enable()
        assert tomllib.loads(_read(cfg_path))["model_providers"]
        assert config_manager.disable()["changed"] is True
        text = _read(cfg_path)
        assert config_manager.START_MARKER not in text
        assert config_manager.END_MARKER not in text
        assert text == user
        assert tomllib.loads(text)["model_providers"]  # user tables still parse

    def test_disable_keeps_content_when_user_root_adjacent_to_table(self, cfg_path) -> None:
        """Semantic preservation when the user wrote root keys flush against a
        table header: the renderer inserts a blank separator (cosmetic), so
        bytes differ but the parsed config is identical.
        """
        user = 'model = "gpt-5.6-luna"\n' + USER_OP
        _write(cfg_path, user)
        config_manager.enable()
        config_manager.disable()
        text = _read(cfg_path)
        assert tomllib.loads(text) == tomllib.loads(user)
        assert config_manager.START_MARKER not in text

    def test_disable_twice_is_byte_idempotent(self, cfg_path) -> None:
        _write(cfg_path, 'model = "gpt-5.6-luna"\n')
        config_manager.enable()
        config_manager.disable()
        first = _read(cfg_path)
        report = config_manager.disable()
        assert report["changed"] is False
        assert _read(cfg_path) == first

    def test_enable_disable_round_trip_restores_provider_tables(self, cfg_path) -> None:
        user = USER_OP + "\n" + USER_ZEN
        _write(cfg_path, user)
        config_manager.enable()
        assert "[model_providers.opencode-go]" in _read(cfg_path)
        config_manager.disable()
        assert _read(cfg_path) == user


class TestUserProviderPreservation:
    def test_user_table_never_duplicated_or_clobbered(self, cfg_path) -> None:
        """Documented behavior: the managed table is omitted, not merged."""
        _write(cfg_path, USER_OP)
        config_manager.enable()
        text = _read(cfg_path)
        assert tomllib.loads(text)["model_providers"]["opencode-go"]["name"] == "OpenCode Go (user)"
        assert text.count("[model_providers.opencode-go]") == 1
        user_section = text.split("[model_providers.opencode-go]", 1)[1]
        assert 'name = "OpenCode Go (user)"' in user_section
        assert 'env_key = "OPENCODE_GO_API_KEY"' in user_section
        assert "8787" not in user_section
        # The managed block sits before the user table and owns the zen side.
        block = _block_text(text)
        assert "[model_providers.opencode-go]" not in block
        assert "[model_providers.zen]" in block
        assert text.index(config_manager.END_MARKER) < text.index("[model_providers.opencode-go]")

    def test_user_multi_agent_v2_configuration_preserved(self, cfg_path) -> None:
        user = "multi_agent_v2 = { enabled = true, num_agents = 3 }\n"
        _write(cfg_path, user)
        config_manager.enable()
        text = _read(cfg_path)
        data = tomllib.loads(text)
        assert data["multi_agent_v2"] == {"enabled": True, "num_agents": 3}
        assert "[features.multi_agent_v2]" not in text
        assert text.startswith(user)


class TestBackupGate:
    def test_backup_written_before_modification(self, cfg_path) -> None:
        """GATE (GATES.md config gate): a backup holds the pre-edit content."""
        user = 'model = "gpt-5.6-luna"\n' + USER_ZEN
        _write(cfg_path, user)
        before = _read(cfg_path)
        config_manager.enable()
        directory = os.path.dirname(cfg_path)
        candidates = [
            name
            for name in os.listdir(directory)
            if name != os.path.basename(cfg_path) and os.path.isfile(os.path.join(directory, name))
        ]
        assert candidates, (
            "no backup file written before enable() modified config.toml "
            "(GATE: backup written before edits)"
        )
        assert any(
            _read(os.path.join(directory, name)) == before for name in candidates
        ), f"backup files {candidates!r} do not contain the pre-edit content"
