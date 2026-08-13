"""Discover opencode models from models.dev and merge them into the local catalog.

The local catalog is a JSON file with a "models" list (see contrib/opencode-go-catalog.json).
models.dev publishes a provider map where providerID "opencode" lists the models
available through the opencode provider. This module fetches that list, compares
it against the local catalog slugs, and reports what would be added or removed.
"""

from __future__ import annotations

import datetime
import json
import os
import time  # noqa: F401
import urllib.error
import urllib.request
from typing import Any

Json = dict[str, Any]

MODELS_DEV_URL = "https://models.dev/api.json"
DEFAULT_TTL_HOURS = 24
CATALOG_REFRESH_ENV = "OPENCODE_GO_CATALOG_REFRESH"


DEFAULT_REASONING_LEVELS = [
    {"effort": "low", "description": "Fast responses with lighter reasoning"},
    {"effort": "medium", "description": "Balances speed and reasoning depth for everyday tasks"},
    {"effort": "high", "description": "Greater reasoning depth for complex problems"},
    {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
]


def _default_model_record() -> dict:
    """Return a compact model record with safe defaults for every render key."""
    return {
        "slug": "",
        "display_name": "",
        "description": "",
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [dict(level) for level in DEFAULT_REASONING_LEVELS],
        "context_window": 1000000,
        "max_context_window": 1000000,
        "input_modalities": ["text"],
        "priority": 50,
        "supports_reasoning_summaries": True,
        "default_reasoning_summary": "none",
        "web_search_tool_type": "text_and_image",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "supports_parallel_tool_calls": True,
        "supports_image_detail_original": False,
        "supports_search_tool": False,
        "effective_context_window_percent": 95,
        "apply_patch_tool_type": "freeform",
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "availability_nux": None,
        "upgrade": None,
        "support_verbosity": False,
        "default_verbosity": "low",
        "experimental_supported_tools": [],
        "use_responses_lite": False,
    }


def _model_from_discovery(m: dict) -> dict:
    """Build a compact model record from a models.dev entry."""
    record = _default_model_record()
    context = m.get("limit", {}).get("context") if isinstance(m.get("limit"), dict) else None
    modalities = m.get("modalities")
    reasoning = m.get("reasoning")
    record.update(
        {
            "slug": m.get("id", ""),
            "display_name": m.get("name", ""),
            "description": m.get("description", ""),
        }
    )
    if isinstance(context, int) and context > 0:
        record["context_window"] = context
        record["max_context_window"] = context
    if isinstance(modalities, list) and modalities:
        record["input_modalities"] = modalities
    if isinstance(reasoning, dict):
        efforts = reasoning.get("efforts")
        if isinstance(efforts, list) and efforts:
            record["supported_reasoning_levels"] = [
                {"effort": effort, "description": f"Reasoning effort {effort}"}
                for effort in efforts
            ]
            record["default_reasoning_level"] = (
                efforts[0] if efforts[0] in {"low", "medium", "high", "xhigh"} else "medium"
            )
    return record


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
    identity = IDENTITY_LINE.format(display_name=display_name)
    if not shared_instructions:
        instructions_template = identity
    else:
        instructions_template = shared_instructions.replace(shared_instructions.splitlines()[0], identity, 1)
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


def parse_iso(value: str) -> datetime.datetime:
    """Parse an ISO timestamp, assuming UTC when no timezone is present."""
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _load_if_readable(path: str) -> dict | None:
    """Load a compact catalog, returning None when it is missing or invalid."""
    if not os.path.exists(path):
        return None
    try:
        return load_compact(path)
    except CatalogDiscoveryError:
        return None


def _render_and_write(compact: dict, catalog_path: str) -> dict:
    """Render a compact catalog, write the full catalog, and return the render."""
    rendered = render_full_catalog(compact)
    write_catalog(compact, catalog_path)
    return rendered


def refresh_catalog(
    compact_path: str = "contrib/opencode-go-models.json",
    catalog_path: str = "contrib/opencode-go-catalog.json",
    now: datetime.datetime | None = None,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    force: bool = False,
) -> dict:
    """Refresh the compact catalog from models.dev when stale, then re-render.

    Returns the rendered full catalog. Refreshing is skipped when the compact
    catalog is fresh (fetched_at within ttl_hours), when the
    OPENCODE_GO_CATALOG_REFRESH env var is "0", or when discovery fails and a
    compact catalog already exists (offline fallback).
    """
    if now is None:
        now = datetime.datetime.now(datetime.UTC)
    iso = now.isoformat()

    if os.environ.get(CATALOG_REFRESH_ENV) == "0":
        compact = load_compact(compact_path)
        return _render_and_write(compact, catalog_path)

    if not force and os.path.exists(compact_path):
        try:
            compact = load_compact(compact_path)
        except CatalogDiscoveryError:
            compact = None
        if compact is not None:
            fetched_at = compact.get("fetched_at")
            if isinstance(fetched_at, str):
                try:
                    fetched = parse_iso(fetched_at)
                    if now - fetched < datetime.timedelta(hours=ttl_hours):
                        return _render_and_write(compact, catalog_path)
                except ValueError:
                    pass

    try:
        discovered = discover_models()
    except CatalogDiscoveryError:
        if os.path.exists(compact_path):
            return _render_and_write(load_compact(compact_path), catalog_path)
        raise

    existing = _load_if_readable(compact_path) or {}
    models = list(existing.get("models", []))
    known = {record.get("slug") for record in models if isinstance(record, dict) and record.get("slug")}
    for entry in discovered:
        model_id = entry.get("id")
        if model_id and model_id not in known:
            models.append(_model_from_discovery(entry))
            known.add(model_id)
    compact = {
        "fetched_at": iso,
        "etag": f'W/"opencode-go-models-{now:%Y%m%d}"',
        "shared_instructions": existing.get("shared_instructions", ""),
        "client_version": existing.get("client_version", ""),
        "models": models,
    }
    with open(compact_path, "w") as f:
        json.dump(compact, f, indent=2)
        f.write("\n")
    return _render_and_write(compact, catalog_path)


__all__ = [
    "CATALOG_REFRESH_ENV",
    "DEFAULT_TTL_HOURS",
    "MODELS_DEV_URL",
    "RENDER_FULL_KEYS",
    "CatalogDiscoveryError",
    "discover_models",
    "load_compact",
    "load_known_slugs",
    "main_refresh",
    "merge_models",
    "parse_iso",
    "refresh_catalog",
    "render_full_catalog",
    "write_catalog",
]


def main_refresh(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="opencode-go-proxy refresh-catalog")
    p.add_argument("--compact", default="contrib/opencode-go-models.json")
    p.add_argument("--catalog", default="contrib/opencode-go-catalog.json")
    p.add_argument("--force", action="store_true")
    p.add_argument("--ttl", type=float, default=DEFAULT_TTL_HOURS)
    args = p.parse_args(argv)
    rendered = refresh_catalog(
        compact_path=args.compact,
        catalog_path=args.catalog,
        ttl_hours=args.ttl,
        force=args.force,
    )
    n = len(rendered.get("models", []))
    size = os.path.getsize(args.catalog)
    print(f"wrote {args.catalog} ({n} models, {size} bytes)")
    return 0
