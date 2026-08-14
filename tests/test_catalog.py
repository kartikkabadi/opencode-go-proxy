import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Self
from unittest import mock

from opencode_go_proxy.catalog import (
    CatalogDiscoveryError,
    CatalogNotModified,
    _model_from_discovery,
    discover_models,
    load_known_slugs,
    merge_models,
    merged_model_slugs,
    model_messages,
    render_full_catalog,
)


class FakeResponse:
    def __init__(self, payload: dict, headers: dict | None = None) -> None:
        self._payload = payload
        self.headers = headers or {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()


def _request_header(request, name: str) -> str | None:
    """Case-insensitive header lookup; urllib capitalizes header keys."""
    return next(
        (value for key, value in request.header_items() if key.lower() == name.lower()),
        None,
    )


class DiscoverModelsTests(unittest.TestCase):
    def test_discover_models_returns_only_opencode_entries(self) -> None:
        payload = {
            "opencode": {
                "models": {
                    "gpt-5.5": {"id": "gpt-5.5", "name": "GPT-5.5", "reasoning": True},
                    "deepseek-v4-flash": {
                        "id": "deepseek-v4-flash",
                        "name": "DeepSeek V4 Flash",
                        "limit": {"context": 400000},
                    },
                }
            },
            "other-provider": {
                "models": {
                    "some-model": {"id": "some-model", "name": "Some Model"},
                }
            },
        }
        seen: dict = {}

        def capture(request, *args, **kwargs):
            seen["url"] = request.full_url
            seen["ua"] = request.headers.get("User-agent")
            seen["if_none_match"] = _request_header(request, "If-None-Match")
            return FakeResponse(payload, headers={"ETag": 'W/"models-dev-v42"'})

        with mock.patch("opencode_go_proxy.catalog.urllib.request.urlopen", side_effect=capture):
            models, etag = discover_models()

        self.assertEqual([m["id"] for m in models], ["gpt-5.5", "deepseek-v4-flash"])
        self.assertNotIn("some-model", {m["id"] for m in models})
        # models.dev rejects the default urllib UA with 403; the discovery
        # fetch must carry an identifying UA.
        self.assertEqual(seen["url"], "https://models.dev/api.json")
        self.assertTrue(seen["ua"].startswith("opencode-go-proxy/"))
        # No stored etag on a first fetch: no conditional header is sent, and
        # the response ETag flows back for the caller to store.
        self.assertIsNone(seen["if_none_match"])
        self.assertEqual(etag, 'W/"models-dev-v42"')

    def test_discover_models_sends_if_none_match_and_raises_on_304(self) -> None:
        seen: dict = {}

        def capture(request, *args, **kwargs):
            seen["if_none_match"] = _request_header(request, "If-None-Match")
            raise urllib.error.HTTPError(request.full_url, 304, "Not Modified", {}, None)

        with mock.patch(
            "opencode_go_proxy.catalog.urllib.request.urlopen", side_effect=capture
        ), self.assertRaises(CatalogNotModified) as ctx:
            discover_models(etag='W/"abc"')

        self.assertEqual(seen["if_none_match"], 'W/"abc"')
        self.assertEqual(ctx.exception.etag, 'W/"abc"')

    def test_discover_models_raises_on_urlopen_failure(self) -> None:
        with mock.patch(
            "opencode_go_proxy.catalog.urllib.request.urlopen",
            side_effect=urllib.error.URLError("boom"),
        ), self.assertRaises(CatalogDiscoveryError):
            discover_models()


class MergeModelsTests(unittest.TestCase):
    def test_existing_plus_new_returns_new_only(self) -> None:
        discovered = [
            {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash"},
            {"id": "new-model", "name": "New Model"},
        ]
        new_models, removed = merge_models({"deepseek-v4-flash"}, discovered)

        self.assertEqual([m["id"] for m in new_models], ["new-model"])
        self.assertEqual(removed, [])

    def test_stale_existing_slug_is_removed(self) -> None:
        discovered = [{"id": "current-model", "name": "Current"}]
        new_models, removed = merge_models({"current-model", "retired-model"}, discovered)

        self.assertEqual(new_models, [])
        self.assertEqual(removed, ["retired-model"])

    def test_disjoint_sets_are_handled(self) -> None:
        discovered = [{"id": "brand-new", "name": "Brand New"}]
        new_models, removed = merge_models({"old-known"}, discovered)

        self.assertEqual([m["id"] for m in new_models], ["brand-new"])
        self.assertEqual(removed, ["old-known"])


class MergedModelSlugsTests(unittest.TestCase):
    """The menu bar polls /v1/models every 3s; the mtime memo must make that
    one stat per poll and re-read only when the merged file actually changes."""

    def _write_merged(self, path: str, slugs: list[str]) -> None:
        with open(path, "w") as f:
            json.dump({"etag": "x", "models": [{"slug": s} for s in slugs]}, f)

    def test_memo_serves_cache_and_rereads_on_mtime_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "merged-models.json")
            with mock.patch("opencode_go_proxy.catalog.merged_models_path", return_value=path):
                # Missing file: empty result, cached under mtime None.
                self.assertEqual(merged_model_slugs(), [])
                # A new file has a new mtime: re-read.
                self._write_merged(path, ["a", "b"])
                self.assertEqual(merged_model_slugs(), ["a", "b"])
                # Same content rewritten at the SAME mtime: served from the
                # cache (no re-read), so the poll stays a single stat.
                st = os.stat(path)
                self._write_merged(path, ["a", "b", "c"])
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
                self.assertEqual(merged_model_slugs(), ["a", "b"])
                # Touching the mtime invalidates the memo: re-read.
                os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1))
                self.assertEqual(merged_model_slugs(), ["a", "b", "c"])
                # Removing the file flips mtime back to None: re-read to empty.
                os.unlink(path)
                self.assertEqual(merged_model_slugs(), [])

    def test_memo_keys_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "one.json")
            second = os.path.join(tmp, "two.json")
            self._write_merged(first, ["a"])
            self._write_merged(second, ["z"])
            with mock.patch("opencode_go_proxy.catalog.merged_models_path", return_value=first):
                self.assertEqual(merged_model_slugs(), ["a"])
            # A different merged file is a different cache slot.
            with mock.patch("opencode_go_proxy.catalog.merged_models_path", return_value=second):
                self.assertEqual(merged_model_slugs(), ["z"])
            # Back to the first file: served from its slot without re-reading.
            with mock.patch("opencode_go_proxy.catalog.merged_models_path", return_value=first):
                self.assertEqual(merged_model_slugs(), ["a"])


class LoadKnownSlugsTests(unittest.TestCase):
    def test_load_known_slugs_reads_temp_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            catalog_path = Path(tmp) / "catalog.json"
            catalog_path.write_text(
                json.dumps({"models": [{"slug": "deepseek-v4-flash"}, {"slug": "deepseek-v4-pro"}]})
            )

            slugs = load_known_slugs(catalog_path=str(catalog_path))

        self.assertEqual(slugs, {"deepseek-v4-flash", "deepseek-v4-pro"})

    def test_load_known_slugs_returns_empty_set_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.json"

            slugs = load_known_slugs(catalog_path=str(missing))

        self.assertEqual(slugs, set())


def minimal_compact(shared_instructions: str, model: dict) -> dict:
    return {
        "fetched_at": "2026-08-10T12:00:00.000000Z",
        "etag": 'W/"opencode-go-models-test"',
        "shared_instructions": shared_instructions,
        "client_version": "0.147.0",
        "models": [model],
    }


def minimal_model(**overrides: object) -> dict:
    record = {
        "slug": "cold-start-model",
        "display_name": "Cold Start Model",
        "description": "Rendered on first discovery.",
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [{"effort": "low", "description": "Lighter"}],
        "context_window": 1000000,
        "max_context_window": 1000000,
    }
    record.update(overrides)
    return record


class ColdStartRenderTests(unittest.TestCase):
    def test_render_with_empty_shared_instructions_does_not_crash(self) -> None:
        compact = minimal_compact("", minimal_model())

        rendered = render_full_catalog(compact)

        model = rendered["models"][0]
        self.assertEqual(model["base_instructions"], "")
        template = model["model_messages"]["instructions_template"]
        self.assertIn("You are Codex, a coding agent based on Cold Start Model.", template)

    def test_model_messages_with_empty_shared_instructions_uses_identity_line(self) -> None:
        messages = model_messages("", "Cold Start Model", 1000000)

        self.assertEqual(
            messages["instructions_template"],
            "You are Codex, a coding agent based on Cold Start Model. You and the user "
            "share one workspace, and your job is to collaborate with them until their "
            "goal is genuinely handled.",
        )
        self.assertNotIn("auto_compact_token_limit", messages)
        self.assertNotIn("multi_agent_version", messages)

    def test_discovery_without_limit_context_keeps_default(self) -> None:
        record = _model_from_discovery({"id": "no-context-model", "name": "No Context"})

        self.assertEqual(record["context_window"], 1000000)
        self.assertEqual(record["max_context_window"], 1000000)

    def test_render_with_default_context_window_does_not_crash(self) -> None:
        discovered = _model_from_discovery({"id": "no-context-model", "name": "No Context"})
        compact = minimal_compact("Line one\n", discovered)

        rendered = render_full_catalog(compact)

        self.assertEqual(
            rendered["models"][0]["auto_compact_token_limit"],
            round(1000000 * 0.9),
        )
        self.assertEqual(rendered["models"][0]["multi_agent_version"], "v2")


if __name__ == "__main__":
    unittest.main()
