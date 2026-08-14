"""Two-strike go-rejection hide: bare go slugs the gateway rejects twice with
its ModelError "not supported" envelope are hidden in the picker so users stop
selecting dead entries (catalog self-cleaning).

The hide reuses the existing hidden-models overlay: model-picker.json under
the state dir (catalog.read_hidden_models / apply_hidden_models), which
render_merged_catalog applies at render time — so a hide write is followed by
one merged-catalog re-render. Strikes are in-memory (proxy lifetime): a
successful go turn clears a slug's strike (and restores a slug this process
auto-hid), and a restart starts with fresh strikes.
"""

import json
import os

import pytest

from opencode_go_proxy import catalog, zen_catalog
from opencode_go_proxy.app import (
    _auto_hidden,
    _go_reject_zen_fallback,
    _unsupported_strikes,
    clear_go_unsupported,
    is_go_not_supported_rejection,
    record_go_unsupported,
)
from opencode_go_proxy.meter import state_dir

DEAD_SLUG = "north-mini-code-free"

# The shape seen live from the go gateway: ModelError with "not supported".
GO_REJECT_BODY = json.dumps(
    {"type": "error", "error": {"type": "ModelError", "message": f"Model {DEAD_SLUG} is not supported"}}
)


def _seed_go_catalog(slug: str = DEAD_SLUG) -> None:
    """Seed the isolated state dir so `slug` is a bare opencode-go catalog
    model the gateway advertises: the state full catalog (known_models()) and
    the state compact (render_merged_catalog()) both list it."""
    state = state_dir()
    os.makedirs(state, exist_ok=True)
    for path in (catalog.state_catalog_path(), catalog.state_compact_path()):
        with open(path, "w") as handle:
            json.dump(
                {
                    "fetched_at": "2026-08-14T00:00:00Z",
                    "etag": "",
                    "client_version": "0.0.0",
                    "shared_instructions": "",
                    "models": [{"slug": slug, "display_name": slug}],
                },
                handle,
            )


def _seed_zen(slug: str) -> None:
    """Seed the zen cache so `slug` is zen-owned (zen_model_ids())."""
    with open(zen_catalog.zen_models_path(), "w") as handle:
        json.dump({"fetched_at": "2026-08-14T00:00:00Z", "models": [{"id": slug}]}, handle)
    zen_catalog._ZEN_MODELS_CACHE = None


def _merged_by_slug() -> dict[str, dict]:
    with open(catalog.merged_models_path()) as handle:
        merged = json.load(handle)
    return {m["slug"]: m for m in merged["models"]}


@pytest.fixture(autouse=True)
def _fresh_strikes():
    """Strikes and auto-hide provenance are proxy-lifetime state; reset per test."""
    _unsupported_strikes.clear()
    _auto_hidden.clear()
    zen_catalog._ZEN_MODELS_CACHE = None
    yield
    _unsupported_strikes.clear()
    _auto_hidden.clear()


class TestTwoStrikeHide:
    def test_first_rejection_only_remembers(self) -> None:
        _seed_go_catalog()
        record_go_unsupported(DEAD_SLUG)

        assert _unsupported_strikes == {DEAD_SLUG: 1}
        # No hide file was written and a render still lists the slug visible.
        assert catalog.read_hidden_models() == set()
        catalog.render_merged_catalog()
        assert _merged_by_slug()[DEAD_SLUG].get("visibility") != "hide"

    def test_second_rejection_hides_in_file_and_render(self) -> None:
        _seed_go_catalog()
        record_go_unsupported(DEAD_SLUG)
        record_go_unsupported(DEAD_SLUG)

        assert _unsupported_strikes == {DEAD_SLUG: 2}
        assert catalog.read_hidden_models() == {DEAD_SLUG}
        # The merged catalog was re-rendered once and now marks the slug hidden.
        assert _merged_by_slug()[DEAD_SLUG]["visibility"] == "hide"

    def test_prefixed_slug_rejected_never_hidden(self) -> None:
        _seed_go_catalog()
        for slug in (f"opencode-go/{DEAD_SLUG}", f"zen/{DEAD_SLUG}"):
            record_go_unsupported(slug)
            record_go_unsupported(slug)

        assert _unsupported_strikes == {}
        assert catalog.read_hidden_models() == set()

    def test_zen_owned_id_rejected_never_hidden(self) -> None:
        _seed_go_catalog()
        _seed_zen(DEAD_SLUG)
        record_go_unsupported(DEAD_SLUG)
        record_go_unsupported(DEAD_SLUG)

        assert _unsupported_strikes == {}
        assert catalog.read_hidden_models() == set()

    def test_slug_outside_go_catalog_never_hidden(self) -> None:
        # A bare slug the go catalog does not advertise (e.g. a native model
        # that reached the go chat path) must never be hidden.
        _seed_go_catalog()
        record_go_unsupported("gpt-5-codex")
        record_go_unsupported("gpt-5-codex")

        assert _unsupported_strikes == {}
        assert catalog.read_hidden_models() == set()

    def test_success_clears_strike(self) -> None:
        _seed_go_catalog()
        record_go_unsupported(DEAD_SLUG)
        record_go_unsupported(DEAD_SLUG)
        assert catalog.read_hidden_models() == {DEAD_SLUG}

        clear_go_unsupported(DEAD_SLUG)
        assert _unsupported_strikes == {}
        assert catalog.read_hidden_models() == set()  # restored: not hidden

        record_go_unsupported(DEAD_SLUG)
        assert _unsupported_strikes == {DEAD_SLUG: 1}
        assert catalog.read_hidden_models() == set()  # one rejection: still not hidden

    def test_restart_resets_strikes(self) -> None:
        _seed_go_catalog()
        record_go_unsupported(DEAD_SLUG)
        record_go_unsupported(DEAD_SLUG)
        assert catalog.read_hidden_models() == {DEAD_SLUG}

        # Process restart: the in-memory strike dict is gone; the hide file
        # persists on disk. One rejection in the new process is strike 1 again.
        _unsupported_strikes.clear()
        _auto_hidden.clear()
        record_go_unsupported(DEAD_SLUG)

        assert _unsupported_strikes == {DEAD_SLUG: 1}
        assert catalog.read_hidden_models() == {DEAD_SLUG}  # durable hide unchanged

    def test_hide_write_idempotent(self) -> None:
        _seed_go_catalog()
        for _ in range(4):
            record_go_unsupported(DEAD_SLUG)

        assert _unsupported_strikes == {DEAD_SLUG: 4}
        # The hide file holds exactly one entry: repeat hides are no-ops.
        assert catalog.read_hidden_models() == {DEAD_SLUG}
        with open(catalog.model_picker_path()) as handle:
            assert json.load(handle) == {"version": 1, "hidden": [DEAD_SLUG]}


class TestRejectionDetector:
    def test_raw_rejection_accepts_live_shape(self) -> None:
        assert is_go_not_supported_rejection(DEAD_SLUG, 401, GO_REJECT_BODY)
        assert is_go_not_supported_rejection(DEAD_SLUG, 403, GO_REJECT_BODY)

    def test_raw_rejection_rejects_other_shapes(self) -> None:
        assert not is_go_not_supported_rejection(DEAD_SLUG, 400, GO_REJECT_BODY)
        assert not is_go_not_supported_rejection("", 401, GO_REJECT_BODY)
        assert not is_go_not_supported_rejection(f"opencode-go/{DEAD_SLUG}", 401, GO_REJECT_BODY)
        assert not is_go_not_supported_rejection(DEAD_SLUG, 401, "not json")
        assert not is_go_not_supported_rejection(DEAD_SLUG, 401, None)

    def test_zen_fallback_requires_zen_ownership(self) -> None:
        # The raw rejection fires for a bare go-catalog slug zen does not own;
        # the zen fallback does not (and must never hide it).
        _seed_go_catalog()
        assert is_go_not_supported_rejection(DEAD_SLUG, 401, GO_REJECT_BODY)
        assert not _go_reject_zen_fallback(DEAD_SLUG, 401, GO_REJECT_BODY)

        _seed_zen(DEAD_SLUG)
        assert _go_reject_zen_fallback(DEAD_SLUG, 401, GO_REJECT_BODY)
