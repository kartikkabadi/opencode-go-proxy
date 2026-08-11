"""Curation CLI: user-models.json overlay write path (plan verification gap)."""
import json
import os

import pytest

from opencode_go_proxy import catalog, models_cmd


@pytest.fixture
def state(tmp_path, monkeypatch) -> str:
    monkeypatch.setenv("OPENCODE_GO_PROXY_STATE_DIR", str(tmp_path))
    return str(tmp_path)


def _overlay(state: str) -> dict:
    path = os.path.join(state, "user-models.json")
    with open(path) as fh:
        return json.load(fh)


def test_add_list_remove_roundtrip(state, capsys) -> None:
    assert models_cmd.models_cmd(["add", "my-custom-model", "--display-name", "My Custom"]) == 0
    data = _overlay(state)
    assert data["version"] == 1
    assert data["models"] == [{"slug": "my-custom-model", "display_name": "My Custom"}]

    assert models_cmd.models_cmd(["list"]) == 0
    out = capsys.readouterr().out
    assert "my-custom-model" in out and "My Custom" in out

    assert models_cmd.models_cmd(["remove", "my-custom-model"]) == 0
    assert _overlay(state)["models"] == []
    assert models_cmd.models_cmd(["remove", "my-custom-model"]) == 0  # no-op


def test_add_rejects_duplicate_without_force(state) -> None:
    assert models_cmd.models_cmd(["add", "m1"]) == 0
    assert models_cmd.models_cmd(["add", "m1"]) != 0
    assert models_cmd.models_cmd(["add", "m1", "--force", "--display-name", "M1 v2"]) == 0
    assert _overlay(state)["models"] == [{"slug": "m1", "display_name": "M1 v2"}]


def test_add_rejects_unknown_key(state) -> None:
    assert models_cmd.models_cmd(["add", "m2", "--set", "bogus_key=1"]) != 0
    path = os.path.join(state, "user-models.json")
    assert not os.path.exists(path)  # nothing written on validation failure


def test_hide_show(state) -> None:
    assert models_cmd.models_cmd(["add", "m3"]) == 0
    assert models_cmd.models_cmd(["hide", "m3"]) == 0
    assert _overlay(state)["models"] == [{"slug": "m3", "hide": True}]
    assert models_cmd.models_cmd(["show", "m3"]) == 0
    assert _overlay(state)["models"] == [{"slug": "m3", "visibility": "list"}]


def test_malformed_file_does_not_crash(state) -> None:
    path = os.path.join(state, "user-models.json")
    with open(path, "w") as fh:
        fh.write("{not json")
    assert models_cmd.models_cmd(["add", "m4"]) == 0
    assert models_cmd.models_cmd(["list"]) == 0


def test_overlay_is_applied_by_catalog(state) -> None:
    models_cmd.models_cmd(["add", "my-custom-model", "--display-name", "My Custom", "--hide"])
    user = catalog.read_user_models()
    merged = catalog.apply_user_models([{"slug": "official"}], user)
    slugs = [m.get("slug") for m in merged]
    assert "my-custom-model" in slugs
    custom = next(m for m in merged if m.get("slug") == "my-custom-model")
    assert custom.get("visibility") == "hide"


def test_hide_preserves_other_overlay_fields(state) -> None:
    models_cmd.models_cmd(["add", "m5", "--display-name", "Keep Me", "--set", "context_window=200000"])
    assert models_cmd.models_cmd(["hide", "m5"]) == 0
    entry = _overlay(state)["models"][0]
    assert entry["display_name"] == "Keep Me"
    assert entry["context_window"] == 200000
    assert entry.get("hide") is True
    assert models_cmd.models_cmd(["show", "m5"]) == 0
    entry = _overlay(state)["models"][0]
    assert entry["display_name"] == "Keep Me"
    assert entry.get("visibility") == "list"
    assert "hide" not in entry


def test_set_parses_numeric_fields(state) -> None:
    assert models_cmd.models_cmd(["add", "m6", "--set", "context_window=200000", "--set", "priority=1.5"]) == 0
    entry = _overlay(state)["models"][0]
    assert entry["context_window"] == 200000
    assert entry["priority"] == 1.5
    assert models_cmd.models_cmd(["add", "m7", "--set", "context_window=abc"]) != 0
