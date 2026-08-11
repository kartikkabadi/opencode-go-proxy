"""Plan 003: local overlay, runtime reload, and state-dir write confinement.

Covers the user-models overlay (add / hide / edit display), hidden-model flags,
announcements, the mtime-cached known_models() reload without restart, and the
rule that runtime refresh never writes the checked-in contrib files.
"""
import json
import os
import socket
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest import mock

import pytest

from opencode_go_proxy import catalog, protocol
from opencode_go_proxy.app import ResponsesProxyHandler
from opencode_go_proxy.config import ProxyConfig


def base_models() -> list[dict]:
    return [
        {
            "slug": "deepseek-v4-flash",
            "display_name": "DeepSeek V4 Flash",
            "description": "Fast and cheap.",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [{"effort": "low", "description": "Lighter"}],
            "context_window": 1000000,
            "max_context_window": 1000000,
            "multi_agent_version": "v1",
            "comp_hash": "opencode-go-deepseek-v4-flash-v1",
        },
        {
            "slug": "deepseek-v4-pro",
            "display_name": "DeepSeek V4 Pro",
            "description": "Stronger reasoning.",
            "default_reasoning_level": "max",
            "supported_reasoning_levels": [{"effort": "max", "description": "Deepest"}],
            "context_window": 400000,
            "max_context_window": 400000,
            "multi_agent_version": "v1",
            "comp_hash": "opencode-go-deepseek-v4-pro-v1",
        },
    ]


def minimal_compact() -> dict:
    return {
        "fetched_at": "2026-08-10T12:00:00.000000Z",
        "etag": 'W/"opencode-go-models-test"',
        "shared_instructions": "Line one\nLine two\n",
        "client_version": "0.147.0",
        "models": base_models(),
    }


class TestApplyUserModels:
    def test_adds_new_model_with_defaults(self) -> None:
        models = catalog.apply_user_models(
            base_models(),
            [{"slug": "curated-flash", "display_name": "Curated Flash", "context_window": 200000}],
        )

        by_slug = {m["slug"]: m for m in models}
        assert by_slug["curated-flash"]["display_name"] == "Curated Flash"
        assert by_slug["curated-flash"]["context_window"] == 200000
        assert by_slug["curated-flash"]["max_context_window"] == 1000000  # default
        assert by_slug["curated-flash"]["multi_agent_version"] == "v1"  # default
        assert len(models) == 3

    def test_edits_display_fields_on_existing_model(self) -> None:
        models = catalog.apply_user_models(
            base_models(),
            [{"slug": "deepseek-v4-flash", "display_name": "Flash (edited)", "priority": 3}],
        )

        flash = models[0]
        assert flash["display_name"] == "Flash (edited)"
        assert flash["priority"] == 3
        assert flash["slug"] == "deepseek-v4-flash"

    def test_hide_flag_sets_visibility(self) -> None:
        models = catalog.apply_user_models(
            base_models(),
            [{"slug": "deepseek-v4-pro", "hide": True}],
        )

        pro = models[1]
        assert pro["visibility"] == "hide"
        assert pro["slug"] == "deepseek-v4-pro"

    def test_visibility_hide_direct_value(self) -> None:
        models = catalog.apply_user_models(
            base_models(),
            [{"slug": "deepseek-v4-pro", "visibility": "hide"}],
        )

        assert models[1]["visibility"] == "hide"

    def test_ignores_entries_without_slug(self) -> None:
        models = catalog.apply_user_models(
            base_models(),
            [{"display_name": "no slug"}, "not-a-dict"],
        )

        assert len(models) == 2

    def test_does_not_mutate_input(self) -> None:
        before = json.dumps(base_models(), sort_keys=True)

        catalog.apply_user_models(
            base_models(),
            [{"slug": "deepseek-v4-flash", "display_name": "Renamed"}],
        )

        assert json.dumps(base_models(), sort_keys=True) == before


class TestHiddenModels:
    def test_apply_hidden_models_hides_slugs(self) -> None:
        result = catalog.apply_hidden_models(base_models(), {"deepseek-v4-flash"})

        assert result[0]["visibility"] == "hide"
        assert result[1].get("visibility") != "hide"

    def test_read_hidden_models_reads_picker_state(self, tmp_path) -> None:
        path = tmp_path / "model-picker.json"
        path.write_text(json.dumps({"version": 1, "hidden": ["a", "b"]}))

        assert catalog.read_hidden_models(str(path)) == {"a", "b"}

    def test_read_hidden_models_empty_when_missing(self, tmp_path) -> None:
        assert catalog.read_hidden_models(str(tmp_path / "missing.json")) == set()


class TestAnnouncements:
    def test_first_run_seeds_silently(self) -> None:
        models, announced = catalog.annotate_announcements(
            base_models(), None, now=1_000_000.0, curated_slugs=set()
        )

        assert all(m.get("availability_nux") is None for m in models)
        assert announced == {"deepseek-v4-flash": 0.0, "deepseek-v4-pro": 0.0}

    def test_new_model_announces_within_window(self) -> None:
        announced = {"deepseek-v4-flash": 1_000_000.0, "deepseek-v4-pro": 1_000_000.0}
        models = base_models() + [
            {
                "slug": "brand-new",
                "display_name": "Brand New",
                "context_window": 400000,
                "supported_reasoning_levels": [{"effort": "low"}, {"effort": "max"}],
                "input_modalities": ["text", "image"],
            }
        ]

        out, next_announced = catalog.annotate_announcements(
            models, announced, now=1_000_100.0, curated_slugs=set()
        )

        new = {m["slug"]: m for m in out}["brand-new"]
        assert new["availability_nux"] == (
            "Brand New just landed in your model picker. It comes with a 400K-token "
            "context window, reasoning efforts from low to max, and image input."
        )
        assert next_announced["brand-new"] == 1_000_100.0

    def test_curated_models_never_announce(self) -> None:
        announced = {"curated-flash": 1_000_000.0}
        models = [
            {"slug": "curated-flash", "display_name": "Curated", "context_window": 400000}
        ]

        out, _ = catalog.annotate_announcements(
            models, announced, now=1_000_100.0, curated_slugs={"curated-flash"}
        )

        assert out[0].get("availability_nux") is None


class TestRenderRuntimeCatalog:
    def test_overlay_pipeline_applies_in_order(self) -> None:
        state = os.environ["OPENCODE_GO_PROXY_STATE_DIR"]
        with open(os.path.join(state, "user-models.json"), "w") as f:
            json.dump(
                {
                    "version": 1,
                    "models": [{"slug": "deepseek-v4-flash", "display_name": "Flash Renamed"}],
                },
                f,
            )
        with open(os.path.join(state, "model-picker.json"), "w") as f:
            json.dump({"version": 1, "hidden": ["deepseek-v4-pro"]}, f)

        rendered = catalog.render_runtime_catalog(minimal_compact())
        by_slug = {m["slug"]: m for m in rendered["models"]}

        assert by_slug["deepseek-v4-flash"]["display_name"] == "Flash Renamed"
        assert by_slug["deepseek-v4-pro"]["visibility"] == "hide"
        # Announcements pass through: availability_nux stays None on first run.
        assert by_slug["deepseek-v4-flash"]["availability_nux"] is None

    def test_render_is_canonical_after_overlay(self) -> None:
        rendered = catalog.render_runtime_catalog(minimal_compact())

        for model in rendered["models"]:
            assert "multi_agent_version" in model
            assert "comp_hash" in model
            assert "model_messages" in model
            assert "approvals" in model["model_messages"]


class TestRuntimeRefresh:
    def test_refresh_writes_only_under_state_dir(self) -> None:
        with open("contrib/opencode-go-models.json") as fh:
            contrib_models = fh.read()
        with open("contrib/opencode-go-catalog.json") as fh:
            contrib_catalog = fh.read()

        with mock.patch("opencode_go_proxy.catalog.discover_models", return_value=[]):
            catalog.refresh_runtime_catalog()

        with open("contrib/opencode-go-models.json") as fh:
            assert fh.read() == contrib_models
        with open("contrib/opencode-go-catalog.json") as fh:
            assert fh.read() == contrib_catalog
        state = os.environ["OPENCODE_GO_PROXY_STATE_DIR"]
        assert os.path.exists(os.path.join(state, "opencode-go-models.json"))
        assert os.path.exists(os.path.join(state, "opencode-go-catalog.json"))
        assert os.path.exists(os.path.join(state, "announced-models.json"))

    def test_known_models_reloads_after_refresh_without_restart(self) -> None:
        protocol.reload_known_models()
        before = set(protocol.known_models())
        assert "curated-flash" not in before

        state = os.environ["OPENCODE_GO_PROXY_STATE_DIR"]
        with open(os.path.join(state, "user-models.json"), "w") as f:
            json.dump(
                {
                    "version": 1,
                    "models": [{"slug": "curated-flash", "display_name": "Curated Flash"}],
                },
                f,
            )
        with mock.patch("opencode_go_proxy.catalog.discover_models", return_value=[]):
            catalog.refresh_runtime_catalog()

        # No explicit reload call: the mtime cache must pick the change up.
        after = set(protocol.known_models())
        assert "curated-flash" in after
        assert len(after) > len(before)

    def test_fresh_compact_skips_discovery_and_reapplies_overlay(self) -> None:
        state = os.environ["OPENCODE_GO_PROXY_STATE_DIR"]
        compact_path = os.path.join(state, "opencode-go-models.json")
        with open(compact_path, "w") as f:
            json.dump(minimal_compact(), f)
        with open(os.path.join(state, "user-models.json"), "w") as f:
            json.dump(
                {"version": 1, "models": [{"slug": "deepseek-v4-flash", "display_name": "Renamed"}]},
                f,
            )

        def fail_if_called(*args, **kwargs):
            raise AssertionError("discover_models must not be called for a fresh catalog")

        with mock.patch("opencode_go_proxy.catalog.discover_models", side_effect=fail_if_called):
            rendered = catalog.refresh_runtime_catalog()

        assert rendered["models"][0]["display_name"] == "Renamed"


@pytest.fixture
def proxy_server():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = ProxyConfig(
        bind="127.0.0.1",
        port=port,
        chat_base_url="https://opencode.ai/zen/go/v1",
        api_key_env="OPENCODE_GO_API_KEY",
        timeout_sec=5,
        max_body_bytes=20 * 1024 * 1024,
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", port), ResponsesProxyHandler)
    httpd.config = config  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield port
    httpd.shutdown()
    httpd.server_close()


def _list_models(port: int) -> list[str]:
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/v1/models")
    resp = conn.getresponse()
    body = json.loads(resp.read())
    conn.close()
    assert resp.status == 200
    return [m["id"] for m in body["data"]]


def test_models_endpoint_grows_after_refresh_without_restart(proxy_server) -> None:
    port = proxy_server
    before = set(_list_models(port))

    state = os.environ["OPENCODE_GO_PROXY_STATE_DIR"]
    with open(os.path.join(state, "user-models.json"), "w") as f:
        json.dump(
            {
                "version": 1,
                "models": [{"slug": "curated-flash", "display_name": "Curated Flash"}],
            },
            f,
        )
    with mock.patch("opencode_go_proxy.catalog.discover_models", return_value=[]):
        catalog.refresh_runtime_catalog()

    after = set(_list_models(port))
    assert "curated-flash" in after
    assert len(after) > len(before)


class TestReviewFixups:
    def test_load_known_slugs_tolerates_non_object_root(self, tmp_path) -> None:
        path = tmp_path / "catalog.json"
        path.write_text("[1, 2, 3]")
        assert catalog.load_known_slugs(catalog_path=str(path)) == set()

    def test_empty_announcement_state_is_not_persisted(self, tmp_path) -> None:
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        seed = catalog.load_seed_compact()
        assert seed is not None
        compact = {**seed, "models": []}
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_STATE_DIR": str(state)}, clear=True), mock.patch(
            "opencode_go_proxy.catalog.read_announced_at", return_value=None
        ):
            catalog.render_runtime_catalog(compact)
        assert not (state / "announced-models.json").exists()

    def test_offline_refresh_falls_back_to_seed(self, tmp_path) -> None:
        state = tmp_path / "state"
        state.mkdir(exist_ok=True)
        seed = catalog.load_seed_compact()
        assert seed is not None
        with mock.patch.dict(os.environ, {"OPENCODE_GO_PROXY_STATE_DIR": str(state)}, clear=True), mock.patch(
            "opencode_go_proxy.catalog.discover_models", side_effect=catalog.CatalogDiscoveryError("offline")
        ), mock.patch("opencode_go_proxy.catalog.load_seed_compact", return_value=seed):
            rendered = catalog.refresh_catalog()
        assert rendered.get("models")
