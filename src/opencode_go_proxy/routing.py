"""Model routing: native Codex models vs the OpenCode Go and Zen translation paths.

Native membership comes from the state-dir native-models.json snapshot
(native_models.capture_native_models); every other slug routes to a
translation path. An explicit ``opencode-go/<slug>`` or ``zen/<slug>`` prefix
always wins over native membership: the user chose the routed provider, and
that request must never reach the native backend. A bare slug that names a
zen-only model (present in the zen capture, absent from the opencode-go
compact catalog) routes to zen — the picker hands those ids over bare, and
sending them to the opencode-go provider 401s. A bare slug the opencode-go
catalog also owns stays on the opencode-go path (go wins for bare
collisions); the ``zen/`` prefix opts a collision into zen.
"""

from __future__ import annotations

import os
from typing import Literal

from . import catalog
from .native_models import load_native_capture, native_models_path, native_slugs
from .zen_catalog import ZEN_PREFIX, zen_model_ids

RouteTarget = Literal["native", "opencode_go", "zen"]

OPENCODE_GO_PREFIX = "opencode-go/"


def normalize_model_slug(slug: str) -> str:
    """Strip the ``opencode-go/`` or ``zen/`` provider prefix; any other slug is unchanged."""
    if slug.startswith(OPENCODE_GO_PREFIX):
        return slug[len(OPENCODE_GO_PREFIX):]
    if slug.startswith(ZEN_PREFIX):
        return slug[len(ZEN_PREFIX):]
    return slug


_NATIVE_SLUGS_CACHE: tuple[str, int | None, frozenset[str]] | None = None


def native_model_slugs() -> set[str]:
    """Native slugs from the snapshot file, cached by mtime; empty when missing.

    Runtime re-capture rewrites the file, so a changed mtime makes the next
    call re-read without a restart.
    """
    global _NATIVE_SLUGS_CACHE
    path = native_models_path()
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        mtime = None
    if (
        _NATIVE_SLUGS_CACHE is not None
        and _NATIVE_SLUGS_CACHE[0] == path
        and _NATIVE_SLUGS_CACHE[1] == mtime
    ):
        return set(_NATIVE_SLUGS_CACHE[2])
    # Prefixed slugs are never native; the filter also covers stale snapshots
    # captured before native_models._native_only() existed.
    slugs = frozenset(s for s in native_slugs(load_native_capture()) if "/" not in s) if mtime is not None else frozenset()
    _NATIVE_SLUGS_CACHE = (path, mtime, slugs)
    return set(slugs)


_GO_COMPACT_CACHE: tuple[str, int | None, str, int | None, frozenset[str]] | None = None


def _go_compact_slugs() -> set[str]:
    """Bare opencode-go slugs from the state-dir compact (else the seed), cached by mtime.

    The go known-model set cannot come from protocol.known_models: protocol
    imports routing, so routing must not import protocol. catalog has no
    routing import, so this helper reads the compact directly and mirrors
    render_merged_catalog's resolution order (state-dir compact, else the
    checked-in seed) so routing and the merged catalog agree on which bare
    slugs the opencode-go provider owns. Both candidate files are keyed by
    mtime, like zen_catalog.zen_model_ids, so a rewritten file is picked up
    without a restart.
    """
    global _GO_COMPACT_CACHE
    state_path = catalog.state_compact_path()
    seed_path = catalog.seed_compact_path()
    state_mtime = _file_mtime(state_path)
    seed_mtime = _file_mtime(seed_path) if seed_path else None
    if (
        _GO_COMPACT_CACHE is not None
        and _GO_COMPACT_CACHE[0] == state_path
        and _GO_COMPACT_CACHE[1] == state_mtime
        and _GO_COMPACT_CACHE[2] == seed_path
        and _GO_COMPACT_CACHE[3] == seed_mtime
    ):
        return set(_GO_COMPACT_CACHE[4])
    compact = catalog.load_runtime_compact()
    if compact is None:
        compact = catalog.load_seed_compact()
    slugs: frozenset[str] = frozenset()
    if compact is not None:
        slugs = frozenset(
            str(entry.get("slug"))
            for entry in compact.get("models", [])
            if isinstance(entry, dict) and entry.get("slug")
        )
    _GO_COMPACT_CACHE = (state_path, state_mtime, seed_path, seed_mtime, slugs)
    return set(slugs)


def _file_mtime(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def route_target(slug: str, native_slugs: set[str] | None = None) -> RouteTarget:
    """Prefix order: ``opencode-go/`` then ``zen/``; bare slugs test native membership.

    A bare slug that names a zen-only model (in the zen capture, absent from
    the opencode-go compact catalog) routes to zen: the picker hands those ids
    over without the ``zen/`` prefix, and the opencode-go provider 401s them.
    A bare slug the opencode-go catalog also owns stays on the opencode-go
    path (go wins for bare collisions); native membership is decided before
    the zen-only rule so a native model never reaches zen.
    """
    if slug.startswith(OPENCODE_GO_PREFIX):
        return "opencode_go"
    if slug.startswith(ZEN_PREFIX):
        return "zen"
    if native_slugs is None:
        native_slugs = native_model_slugs()
    if slug in native_slugs:
        return "native"
    if slug in zen_model_ids() and slug not in _go_compact_slugs():
        return "zen"
    return "opencode_go"
