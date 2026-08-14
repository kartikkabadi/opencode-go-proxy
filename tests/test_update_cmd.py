"""Tests for the `update` CLI command (ops.update_cmd).

The network + cache module (`opencode_go_proxy.updates`) is faked through
sys.modules injection so these tests never touch the network and still pass
before updates.py exists on disk.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types

import pytest

import opencode_go_proxy
from opencode_go_proxy import ops

ONE_LINER = "uvx --from git+https://github.com/kartikkabadi/opencode-go-proxy@v0.4.6 opencode-go-proxy"
RELEASE_URL = "https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.4.6"


class FakeUpdateInfo:
    def __init__(
        self,
        current: str = "0.4.0",
        latest: str = "0.4.6",
        available: bool = True,
        release_url: str | None = RELEASE_URL,
        checked_at: str = "2026-08-14T12:00:00Z",
        error: str | None = None,
    ):
        self.current = current
        self.latest = latest
        self.available = available
        self.release_url = release_url
        self.checked_at = checked_at
        self.error = error


def _install_fake_updates(monkeypatch: pytest.MonkeyPatch, info: FakeUpdateInfo | None = None) -> dict:
    module = types.ModuleType("opencode_go_proxy.updates")
    module.UpdateInfo = FakeUpdateInfo
    current_info = info if info is not None else FakeUpdateInfo()
    calls: list = []
    def check_for_updates(force: bool = False, timeout: int = 8) -> FakeUpdateInfo:
        calls.append(("check", force, timeout))
        return current_info

    def version_payload(force: bool = False) -> dict:
        calls.append(("payload", force))
        return {
            "version": current_info.current,
            "git_commit": "deadbeef",
            "update": {
                "latest": current_info.latest,
                "available": current_info.available,
                "release_url": current_info.release_url,
                "checked_at": current_info.checked_at,
                "error": current_info.error,
            },
        }

    module.check_for_updates = check_for_updates
    module.version_payload = version_payload
    monkeypatch.setitem(sys.modules, "opencode_go_proxy.updates", module)
    # The real updates module may already be loaded by an earlier test (for
    # example test_state.py imports it at module level), which leaves the
    # package carrying the `updates` attribute. `from . import updates` inside
    # ops then short-circuits through the package attribute and never consults
    # sys.modules, so patch the attribute too (raising=False: it may not exist).
    monkeypatch.setattr(opencode_go_proxy, "updates", module, raising=False)
    return {"info": current_info, "calls": calls}


def _patch_which(monkeypatch: pytest.MonkeyPatch, uv: str | None) -> None:
    monkeypatch.setattr(ops.shutil, "which", lambda name: uv if name == "uv" else None)


def _patch_run(monkeypatch: pytest.MonkeyPatch, fake) -> list:
    calls: list = []

    def run(cmd, *args, **kwargs):
        calls.append((cmd, kwargs))
        return fake(cmd, *args, **kwargs)

    monkeypatch.setattr(ops.subprocess, "run", run)
    return calls


def test_check_available_prints_versions_and_exits_3(monkeypatch, capsys):
    _install_fake_updates(monkeypatch)
    assert ops.update_cmd([]) == 3
    out = capsys.readouterr().out
    assert "current 0.4.0 / latest 0.4.6" in out
    assert RELEASE_URL in out


def test_check_up_to_date_exits_0(monkeypatch, capsys):
    _install_fake_updates(monkeypatch, info=FakeUpdateInfo(latest="0.4.0", available=False, release_url=None))
    assert ops.update_cmd([]) == 0
    assert "up to date" in capsys.readouterr().out


def test_check_error_exits_1(monkeypatch, capsys):
    _install_fake_updates(monkeypatch, info=FakeUpdateInfo(error="github api unreachable", available=False))
    assert ops.update_cmd([]) == 1
    assert "github api unreachable" in capsys.readouterr().err


def test_check_force_passthrough(monkeypatch):
    state = _install_fake_updates(monkeypatch)
    assert ops.update_cmd(["--force"]) == 3
    assert state["calls"] == [("check", True, 8)]


def test_json_emits_version_payload_shape_and_keeps_exit_semantics(monkeypatch, capsys):
    _install_fake_updates(monkeypatch)
    assert ops.update_cmd(["--json"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert sorted(payload.keys()) == ["git_commit", "update", "version"]
    assert sorted(payload["update"].keys()) == [
        "available",
        "checked_at",
        "error",
        "latest",
        "release_url",
    ]


def test_json_up_to_date_exits_0(monkeypatch, capsys):
    _install_fake_updates(monkeypatch, info=FakeUpdateInfo(latest="0.4.0", available=False))
    assert ops.update_cmd(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["update"]["available"] is False


def test_apply_uv_tool_runs_exact_install_argv(monkeypatch, capsys):
    _install_fake_updates(monkeypatch)
    _patch_which(monkeypatch, "/opt/homebrew/bin/uv")

    def fake_run(cmd, *args, **kwargs):
        if cmd[2] == "list":
            return subprocess.CompletedProcess(cmd, 0, stdout="opencode-go-proxy v0.4.0\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="Installed 1 package\n")

    calls = _patch_run(monkeypatch, fake_run)
    assert ops.update_cmd(["--apply"]) == 0
    installs = [call for call in calls if call[0][2] == "install"]
    assert len(installs) == 1
    assert installs[0][0] == [
        "/opt/homebrew/bin/uv",
        "tool",
        "install",
        "--force",
        "--from",
        "git+https://github.com/kartikkabadi/opencode-go-proxy@v0.4.6",
        "opencode-go-proxy",
    ]
    assert "installed opencode-go-proxy 0.4.6" in capsys.readouterr().out


def test_apply_ephemeral_prints_one_liner_without_spawning_uv(monkeypatch, capsys):
    _install_fake_updates(monkeypatch)
    _patch_which(monkeypatch, None)

    def boom(*args, **kwargs):
        pytest.fail("subprocess.run must not be called on the ephemeral path")

    _patch_run(monkeypatch, boom)
    assert ops.update_cmd(["--apply"]) == 0
    assert ONE_LINER in capsys.readouterr().out


def test_apply_uv_missing_prints_one_liner(monkeypatch, capsys):
    _install_fake_updates(monkeypatch)
    monkeypatch.setattr(ops.shutil, "which", lambda name: None)
    _patch_run(monkeypatch, lambda *a, **kw: pytest.fail("subprocess.run must not be called"))
    assert ops.update_cmd(["--apply"]) == 0
    assert ONE_LINER in capsys.readouterr().out


def test_apply_not_a_uv_tool_prints_one_liner(monkeypatch, capsys):
    _install_fake_updates(monkeypatch)
    _patch_which(monkeypatch, "/usr/bin/uv")
    listing = subprocess.CompletedProcess([], 0, stdout="python 3.12\n")
    _patch_run(monkeypatch, lambda cmd, *a, **kw: listing)
    assert ops.update_cmd(["--apply"]) == 0
    assert ONE_LINER in capsys.readouterr().out


def test_apply_install_failure_exits_1(monkeypatch, capsys):
    _install_fake_updates(monkeypatch)
    _patch_which(monkeypatch, "/opt/homebrew/bin/uv")

    def fake_run(cmd, *args, **kwargs):
        if cmd[2] == "list":
            return subprocess.CompletedProcess(cmd, 0, stdout="opencode-go-proxy\n")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="build failed")

    _patch_run(monkeypatch, fake_run)
    assert ops.update_cmd(["--apply"]) == 1
    assert "install failed" in capsys.readouterr().err


def test_apply_up_to_date_exits_0(monkeypatch, capsys):
    _install_fake_updates(monkeypatch, info=FakeUpdateInfo(latest="0.4.0", available=False))
    assert ops.update_cmd(["--apply"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_apply_check_error_exits_1(monkeypatch, capsys):
    _install_fake_updates(monkeypatch, info=FakeUpdateInfo(error="network down", available=False))
    assert ops.update_cmd(["--apply"]) == 1
    assert "network down" in capsys.readouterr().err
