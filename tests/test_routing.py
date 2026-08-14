"""Routing policy: prefix normalization and native vs opencode-go vs zen targets."""

import json
import os
import unittest

from opencode_go_proxy import zen_catalog
from opencode_go_proxy.meter import state_dir
from opencode_go_proxy.routing import normalize_model_slug, route_target


def _write_native_capture(slugs: list[str]) -> str:
    state = state_dir()
    os.makedirs(state, exist_ok=True)
    path = os.path.join(state, "native-models.json")
    models = [
        {
            "slug": slug,
            "display_name": slug,
            "supported_reasoning_levels": [{"effort": "medium", "description": "m"}],
        }
        for slug in slugs
    ]
    with open(path, "w") as handle:
        json.dump({"captured_at": "2026-08-13T00:00:00Z", "models": models}, handle)
    return path


def _seed_zen_ids(model_ids: list[str]) -> None:
    """Write the zen capture file so zen_model_ids() sees these bare ids."""
    state = state_dir()
    os.makedirs(state, exist_ok=True)
    with open(zen_catalog.zen_models_path(), "w") as handle:
        json.dump(
            {"fetched_at": "2026-08-14T00:00:00Z", "models": [{"id": model_id} for model_id in model_ids]},
            handle,
        )
    zen_catalog._ZEN_MODELS_CACHE = None


class NormalizeModelSlugTests(unittest.TestCase):
    def test_strips_opencode_go_prefix(self) -> None:
        self.assertEqual(normalize_model_slug("opencode-go/deepseek-v4-pro"), "deepseek-v4-pro")

    def test_bare_slug_unchanged(self) -> None:
        self.assertEqual(normalize_model_slug("deepseek-v4-pro"), "deepseek-v4-pro")

    def test_native_slug_unchanged(self) -> None:
        self.assertEqual(normalize_model_slug("gpt-5.6-luna"), "gpt-5.6-luna")

    def test_other_provider_prefixes_unchanged(self) -> None:
        self.assertEqual(
            normalize_model_slug("opencode-go-messages/qwen3.8-max"),
            "opencode-go-messages/qwen3.8-max",
        )


class RouteTargetTests(unittest.TestCase):
    def test_native_slug_routes_native(self) -> None:
        _write_native_capture(["gpt-5.6-luna", "gpt-5.5"])
        self.assertEqual(route_target("gpt-5.6-luna"), "native")

    def test_prefixed_opencode_go_slug_routes_opencode_go(self) -> None:
        _write_native_capture(["gpt-5.6-luna"])
        self.assertEqual(route_target("opencode-go/deepseek-v4-flash"), "opencode_go")

    def test_unknown_slug_routes_opencode_go(self) -> None:
        _write_native_capture(["gpt-5.6-luna"])
        self.assertEqual(route_target("no-such-model"), "opencode_go")

    def test_missing_capture_routes_everything_opencode_go(self) -> None:
        self.assertEqual(route_target("gpt-5.6-luna"), "opencode_go")
        self.assertEqual(route_target("opencode-go/deepseek-v4-flash"), "opencode_go")

    def test_explicit_native_slugs_override_file(self) -> None:
        self.assertEqual(route_target("x-model", native_slugs={"x-model"}), "native")
        self.assertEqual(route_target("y-model", native_slugs={"x-model"}), "opencode_go")

    def test_prefixed_native_entry_never_hijacks_bare_app_slug(self) -> None:
        # The native capture lists opencode-go/deepseek-v4-flash; the app's
        # own references to that model must still route to opencode_go after
        # the prefix is stripped.
        _write_native_capture(["gpt-5.6-luna", "opencode-go/deepseek-v4-flash"])
        self.assertEqual(route_target("opencode-go/deepseek-v4-flash"), "opencode_go")
        self.assertEqual(route_target("deepseek-v4-flash"), "opencode_go")

    def test_prefixed_slug_wins_over_bare_native_collision(self) -> None:
        # A user-selected opencode-go/<slug> whose bare slug is a native model
        # must stay on the translation path: its Authorization header must
        # never reach the native backend.
        _write_native_capture(["gpt-5.6-luna"])
        self.assertEqual(route_target("opencode-go/gpt-5.6-luna"), "opencode_go")

    def test_capture_update_picked_up_by_mtime(self) -> None:
        path = _write_native_capture(["gpt-5.6-luna"])
        self.assertEqual(route_target("gpt-5.6-luna"), "native")
        old_mtime = os.stat(path).st_mtime_ns
        with open(path, "w") as handle:
            json.dump({"captured_at": "2026-08-13T00:00:00Z", "models": [{"slug": "gpt-5.5"}]}, handle)
        os.utime(path, ns=(old_mtime + 10**9, old_mtime + 10**9))
        # Same path, newer content: the mtime cache must re-read.
        self.assertEqual(route_target("gpt-5.6-luna"), "opencode_go")
        self.assertEqual(route_target("gpt-5.5"), "native")


class ZenRouteTargetTests(unittest.TestCase):
    def test_zen_prefix_routes_zen(self) -> None:
        _write_native_capture(["gpt-5.6-luna"])
        self.assertEqual(route_target("zen/gpt-5.5"), "zen")
        self.assertEqual(route_target("zen/claude-sonnet-4-5"), "zen")

    def test_bare_zen_only_id_routes_zen(self) -> None:
        # north-mini-code-free is served by zen but absent from the
        # opencode-go compact catalog; a bare picker selection routes zen.
        _write_native_capture(["gpt-5.6-luna"])
        _seed_zen_ids(["north-mini-code-free"])
        self.assertEqual(route_target("north-mini-code-free"), "zen")

    def test_bare_collision_id_stays_opencode_go(self) -> None:
        # deepseek-v4-flash exists on both zen and the opencode-go catalog:
        # go wins for bare slugs; the zen/ prefix opts a collision into zen.
        _write_native_capture(["gpt-5.6-luna"])
        _seed_zen_ids(["deepseek-v4-flash", "north-mini-code-free"])
        self.assertEqual(route_target("deepseek-v4-flash"), "opencode_go")
        self.assertEqual(route_target("north-mini-code-free"), "zen")

    def test_bare_zen_only_id_that_is_native_stays_native(self) -> None:
        # Native membership is decided before the zen-only rule.
        _write_native_capture(["north-mini-code-free"])
        _seed_zen_ids(["north-mini-code-free"])
        self.assertEqual(route_target("north-mini-code-free"), "native")

    def test_go_compact_slugs_refresh_by_mtime(self) -> None:
        # The go-side set is mtime-cached: adding a slug to the state compact
        # moves a zen-only id onto the opencode-go path, and removing it moves
        # it back, without a restart.
        from opencode_go_proxy import catalog

        _seed_zen_ids(["zen-only-test-model"])
        self.assertEqual(route_target("zen-only-test-model"), "zen")

        path = catalog.state_compact_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        compact = {
            "fetched_at": "2026-08-14T00:00:00Z",
            "etag": "",
            "client_version": "0.147.0",
            "shared_instructions": "",
            "models": [{"slug": "zen-only-test-model", "display_name": "Zen Only Test"}],
        }
        with open(path, "w") as handle:
            json.dump(compact, handle)
        os.utime(path, ns=(os.stat(path).st_mtime_ns + 10**9, os.stat(path).st_mtime_ns + 10**9))
        # Same path, newer content: the mtime cache must re-read the go set.
        self.assertEqual(route_target("zen-only-test-model"), "opencode_go")

        compact["models"] = []
        with open(path, "w") as handle:
            json.dump(compact, handle)
        os.utime(path, ns=(os.stat(path).st_mtime_ns + 10**9, os.stat(path).st_mtime_ns + 10**9))
        self.assertEqual(route_target("zen-only-test-model"), "zen")

    def test_opencode_go_prefix_wins_over_zen(self) -> None:
        _write_native_capture(["gpt-5.6-luna"])
        self.assertEqual(route_target("opencode-go/claude-sonnet-4-5"), "opencode_go")
        self.assertEqual(route_target("opencode-go/gpt-5.5"), "opencode_go")

    def test_zen_prefix_wins_over_bare_native_collision(self) -> None:
        # A user-selected zen/<slug> whose bare slug is a native model must
        # stay on the zen path: it must never reach the native backend.
        _write_native_capture(["gpt-5.6-luna"])
        self.assertEqual(route_target("zen/gpt-5.6-luna"), "zen")

    def test_native_unchanged_with_zen_routes(self) -> None:
        _write_native_capture(["gpt-5.6-luna"])
        self.assertEqual(route_target("gpt-5.6-luna"), "native")
        self.assertEqual(route_target("zen/gpt-5.6-luna"), "zen")
        self.assertEqual(route_target("opencode-go/gpt-5.6-luna"), "opencode_go")


class NormalizeZenSlugTests(unittest.TestCase):
    def test_strips_zen_prefix(self) -> None:
        self.assertEqual(normalize_model_slug("zen/gpt-5.5"), "gpt-5.5")
        self.assertEqual(normalize_model_slug("zen/claude-sonnet-4-5"), "claude-sonnet-4-5")

    def test_opencode_go_prefix_still_stripped(self) -> None:
        self.assertEqual(normalize_model_slug("opencode-go/deepseek-v4-flash"), "deepseek-v4-flash")

    def test_prefixes_do_not_cross_contaminate(self) -> None:
        self.assertEqual(normalize_model_slug("opencode-go-zen/qwen3"), "opencode-go-zen/qwen3")
        self.assertEqual(normalize_model_slug("zen-opencode-go/gpt-5.5"), "zen-opencode-go/gpt-5.5")
        self.assertEqual(normalize_model_slug("zen/deepseek-v4-flash"), "deepseek-v4-flash")


if __name__ == "__main__":
    unittest.main()
