"""Discover opencode models from models.dev and merge them into the local catalog.

The local catalog is a JSON file with a "models" list (see contrib/opencode-go-catalog.json).
models.dev publishes a provider map where providerID "opencode" lists the models
available through the opencode provider. This module fetches that list, compares
it against the local catalog slugs, and reports what would be added or removed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

Json = dict[str, Any]

MODELS_DEV_URL = "https://models.dev/api.json"


class CatalogDiscoveryError(Exception):
    """Raised when the models.dev catalog cannot be fetched or parsed."""


def discover_models(timeout: int = 10) -> list[dict]:
    """Return model dicts from models.dev whose providerID is "opencode".

    Each returned dict is a models.dev model entry (id, name, description,
    context/modalities/reasoning/cost when present). Raises
    CatalogDiscoveryError on network failure or malformed JSON.
    """
    try:
        with urllib.request.urlopen(MODELS_DEV_URL, timeout=timeout) as resp:
            payload: Json = json.load(resp)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise CatalogDiscoveryError(f"failed to fetch {MODELS_DEV_URL}: {exc}") from exc

    provider = payload.get("opencode")
    if not isinstance(provider, dict):
        return []
    models = provider.get("models")
    if isinstance(models, dict):
        return [entry for entry in models.values() if isinstance(entry, dict)]
    if isinstance(models, list):
        return [entry for entry in models if isinstance(entry, dict)]
    return []


def load_known_slugs(catalog_path: str | None = None) -> set[str]:
    """Return the set of model slugs in the local catalog.

    Resolves the catalog path the same way protocol.py does: CODEX_MODEL_CATALOG
    env var, else ~/.codex/model-catalogs/opencode-go.json. A missing or
    malformed file yields an empty set.
    """
    if catalog_path is None:
        catalog_path = os.environ.get(
            "CODEX_MODEL_CATALOG", os.path.expanduser("~/.codex/model-catalogs/opencode-go.json")
        )
    try:
        with open(catalog_path) as f:
            catalog = json.load(f)
        return {m["slug"] for m in catalog.get("models", []) if isinstance(m, dict) and "slug" in m}
    except (OSError, json.JSONDecodeError, KeyError):
        return set()


def merge_models(existing_slugs: set[str], discovered: list[dict]) -> tuple[list[dict], list[str]]:
    """Compute the diff between local slugs and discovered models.

    Returns (new_models, removed_slugs): entries whose id is not already in
    existing_slugs, and existing slugs with no discovered match, sorted.
    """
    discovered_ids = {entry.get("id") for entry in discovered if isinstance(entry, dict) and entry.get("id")}
    new_models = [entry for entry in discovered if isinstance(entry, dict) and entry.get("id") not in existing_slugs]
    removed_slugs = sorted(existing_slugs - discovered_ids)
    return new_models, removed_slugs


__all__ = [
    "MODELS_DEV_URL",
    "CatalogDiscoveryError",
    "discover_models",
    "load_known_slugs",
    "merge_models",
]
