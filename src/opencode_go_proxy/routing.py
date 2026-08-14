"""Model routing: native Codex models vs the OpenCode Go and Zen translation paths.

Native membership comes from the state-dir native-models.json snapshot
(native_models.capture_native_models); every other slug routes to a
translation path. An explicit ``opencode-go/<slug>`` or ``zen/<slug>`` prefix
always wins over native membership: the user chose the routed provider, and
that request must never reach the native backend. Bare slugs are never zen —
zen models are only reachable with the ``zen/`` prefix, so a bare
deepseek-v4-flash stays on the opencode-go path even though zen serves the
same id.
"""

from __future__ import annotations

import os
from typing import Literal

from .native_models import load_native_capture, native_models_path, native_slugs
from .zen_catalog import ZEN_PREFIX

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


def route_target(slug: str, native_slugs: set[str] | None = None) -> RouteTarget:
    """Prefix order: ``opencode-go/`` then ``zen/``; bare slugs test native membership.

    Bare slugs never route zen: a zen model must be addressed with its
    ``zen/`` prefix, and an unprefixed slug that happens to share a zen id
    stays on the opencode-go path.
    """
    if slug.startswith(OPENCODE_GO_PREFIX):
        return "opencode_go"
    if slug.startswith(ZEN_PREFIX):
        return "zen"
    if native_slugs is None:
        native_slugs = native_model_slugs()
    return "native" if slug in native_slugs else "opencode_go"
