"""Routing policy: prefix normalization and native vs opencode-go targets."""

import json
import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
