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


# Keys carried through from the compact record into each rendered model. Any
# key outside this list is dropped from the full-shape output.
RENDER_FULL_KEYS = (
    "shell_type",
    "visibility",
    "supported_in_api",
    "priority",
    "additional_speed_tiers",
    "service_tiers",
    "availability_nux",
    "upgrade",
    "supports_reasoning_summaries",
    "default_reasoning_summary",
    "support_verbosity",
    "default_verbosity",
    "apply_patch_tool_type",
    "web_search_tool_type",
    "truncation_policy",
    "supports_parallel_tool_calls",
    "supports_image_detail_original",
    "context_window",
    "max_context_window",
    "effective_context_window_percent",
    "experimental_supported_tools",
    "input_modalities",
    "supports_search_tool",
    "use_responses_lite",
)

IDENTITY_LINE = (
    "You are Codex, a coding agent based on {display_name}. You and the user "
    "share one workspace, and your job is to collaborate with them until their "
    "goal is genuinely handled."
)


def model_messages(shared_instructions: str, display_name: str, context_window: int) -> dict[str, Any]:
    """Build the model_messages dict Codex requires for a rendered model."""
    instructions_template = shared_instructions.replace(
        shared_instructions.splitlines()[0],
        IDENTITY_LINE.format(display_name=display_name),
        1,
    )
    return {
        "instructions_template": instructions_template,
        "supports_reasoning_summaries": True,
        "auto_compact_token_limit": round(context_window * 0.9),
        "multi_agent_version": "v1",
    }


def render_full_catalog(compact: dict) -> dict:
    """Render the compact catalog to the full-shape catalog Codex consumes.

    Each compact model gains base_instructions (shared once at the top level)
    and model_messages built by model_messages(). The input compact dict is
    not mutated.
    """
    shared_instructions = compact["shared_instructions"]
    models = []
    for record in compact.get("models", []):
        full: dict[str, Any] = {key: record[key] for key in RENDER_FULL_KEYS if key in record}
        full["slug"] = record["slug"]
        full["display_name"] = record["display_name"]
        full["description"] = record["description"]
        full["default_reasoning_level"] = record["default_reasoning_level"]
        full["supported_reasoning_levels"] = record["supported_reasoning_levels"]
        full["base_instructions"] = shared_instructions
        full["model_messages"] = model_messages(
            shared_instructions, record["display_name"], int(record["context_window"])
        )
        models.append(full)
    return {
        "fetched_at": compact["fetched_at"],
        "etag": compact["etag"],
        "client_version": compact["client_version"],
        "models": models,
    }


def load_compact(path: str) -> dict:
    """Load a compact catalog JSON file, raising CatalogDiscoveryError on failure."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogDiscoveryError(f"failed to load compact catalog {path}: {exc}") from exc


def write_catalog(compact: dict, out_path: str) -> None:
    """Render the compact catalog and write the full-shape catalog to out_path."""
    rendered = render_full_catalog(compact)
    with open(out_path, "w") as f:
        json.dump(rendered, f, indent=2)
        f.write("\n")


__all__ = [
    "MODELS_DEV_URL",
    "RENDER_FULL_KEYS",
    "CatalogDiscoveryError",
    "discover_models",
    "load_compact",
    "load_known_slugs",
    "merge_models",
    "render_full_catalog",
    "write_catalog",
]
