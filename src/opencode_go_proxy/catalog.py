"""Discover opencode models and render the full-shape catalog Codex consumes.

The compact catalog (a JSON file with a "models" list, see
contrib/opencode-go-models.json) is the checked-in seed. models.dev publishes a
provider map where providerID "opencode" lists the models available through the
opencode provider; discovery additively merges those entries into the seed.

The runtime pipeline is layered:

1. Seed/merge: state-dir compact (else checked-in seed) plus models.dev
   discovery, TTL-gated so a fresh catalog never hits the network.
2. Overlay: user-models.json overrides (add / hide / edit display), then
   availability announcements, then hidden-model flags.
3. Render: the overlay result is projected through the canonical model shape
   (CANONICAL_MODEL_KEYS / MODEL_MESSAGES_KEYS, matching codex-router's
   merged-models.json key set) and written under the state dir only.

Runtime writes never leave OPENCODE_GO_PROXY_STATE_DIR (default
~/.codex/opencode-go-proxy/); the checked-in contrib files are maintained by
the explicit `refresh-catalog` CLI instead.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any

from .meter import state_dir

Json = dict[str, Any]

MODELS_DEV_URL = "https://models.dev/api.json"
DEFAULT_TTL_HOURS = 24
CATALOG_REFRESH_ENV = "OPENCODE_GO_CATALOG_REFRESH"
SEED_CATALOG_ENV = "OPENCODE_GO_PROXY_SEED_CATALOG"
STATE_CATALOG_NAME = "opencode-go-catalog.json"
STATE_COMPACT_NAME = "opencode-go-models.json"
USER_MODELS_NAME = "user-models.json"
MODEL_PICKER_NAME = "model-picker.json"
ANNOUNCED_MODELS_NAME = "announced-models.json"

# Codex auto-announces a model (availability_nux card) for this long after it
# first appears, mirroring codex-router's announcement window.
AUTO_ANNOUNCE_WINDOW_SECONDS = 7 * 24 * 60 * 60


DEFAULT_REASONING_LEVELS = [
    {"effort": "low", "description": "Fast responses with lighter reasoning"},
    {"effort": "medium", "description": "Balances speed and reasoning depth for everyday tasks"},
    {"effort": "high", "description": "Greater reasoning depth for complex problems"},
    {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
]


def _canonical_model_defaults() -> dict[str, Any]:
    """Return a fresh full-shape model with a default for every key Codex reads.

    This is the canonical model contract: the exact merged key set sampled from
    codex-router's merged-models.json. render_full_catalog projects from this
    shape, so no key is hand-copied and a key added upstream can never silently
    drop out of the rendered catalog. Keys marked in OPTIONAL_MODEL_KEYS may be
    omitted by compact records; the remaining keys always carry real values.
    """
    return {
        "additional_speed_tiers": [],
        "apply_patch_tool_type": "freeform",
        "auto_compact_token_limit": None,  # computed from context_window at render
        "availability_nux": None,
        "base_instructions": "",  # computed from shared_instructions at render
        "comp_hash": "",
        "context_window": 1000000,
        "default_reasoning_level": "medium",
        "default_reasoning_summary": "none",
        "default_service_tier": None,
        "default_verbosity": "low",
        "description": "",
        "display_name": "",
        "effective_context_window_percent": 95,
        "experimental_supported_tools": [],
        "include_plugin_usage_instructions": True,
        "include_skills_usage_instructions": True,
        "input_modalities": ["text"],
        "max_context_window": 1000000,
        "model_messages": {},  # computed at render
        "multi_agent_version": "v1",
        "priority": 50,
        "service_tiers": [],
        "shell_type": "shell_command",
        "slug": "",
        "support_verbosity": False,
        "supported_in_api": True,
        "supported_reasoning_levels": [dict(level) for level in DEFAULT_REASONING_LEVELS],
        "supports_image_detail_original": False,
        "supports_parallel_tool_calls": True,
        "supports_reasoning_summaries": True,
        "supports_search_tool": False,
        "tool_mode": None,
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "upgrade": None,
        "use_responses_lite": False,
        "visibility": "list",
        "web_search_tool_type": "text_and_image",
    }


# Ordered canonical key set; used by the renderer as the projection source.
CANONICAL_MODEL_KEYS: tuple[str, ...] = tuple(_canonical_model_defaults().keys())


# The exact key set Codex reads inside model_messages (merged-models.json).
MODEL_MESSAGES_KEYS: tuple[str, ...] = (
    "approvals",
    "auto_review",
    "collaboration_modes",
    "instructions_template",
    "instructions_variables",
    "permissions",
    "token_budget",
)


# Compact records may omit any key whose canonical default is safe. This set is
# the escape hatch for upstream fields the proxy does not compute: they stay in
# the canonical shape and render from their default, never silently drop.
OPTIONAL_MODEL_KEYS: frozenset[str] = frozenset(CANONICAL_MODEL_KEYS) - frozenset(
    {
        "slug",
        "display_name",
        "description",
        "context_window",
        "max_context_window",
        "default_reasoning_level",
        "supported_reasoning_levels",
        "multi_agent_version",
        "comp_hash",
    }
)


# Keys the user overlay (user-models.json) may set on a compact record: the
# canonical set minus the renderer-computed instruction fields.
OVERLAY_EDIT_KEYS: frozenset[str] = frozenset(CANONICAL_MODEL_KEYS) - frozenset(
    {"base_instructions", "model_messages"}
)


def _comp_hash_for(slug: str) -> str:
    """Stable per-slug compatibility hash, in the same style as codex-router."""
    return f"opencode-go-{slug.replace('/', '-')}-v1"


def _default_model_record() -> dict:
    """Return a compact model record: the canonical shape minus computed keys."""
    defaults = _canonical_model_defaults()
    for key in ("base_instructions", "model_messages", "auto_compact_token_limit"):
        defaults.pop(key, None)
    return defaults


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
            "comp_hash": _comp_hash_for(m.get("id", "")),
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


def default_catalog_path() -> str:
    """Return the full-shape catalog the proxy serves.

    CODEX_MODEL_CATALOG wins when set; otherwise the state-dir catalog that
    runtime refresh writes (OPENCODE_GO_PROXY_STATE_DIR, default
    ~/.codex/opencode-go-proxy/).
    """
    env = os.environ.get("CODEX_MODEL_CATALOG")
    if env:
        return env
    return state_catalog_path()


def load_known_slugs(catalog_path: str | None = None) -> set[str]:
    """Return the set of model slugs in the full-shape catalog.

    Resolves the catalog path the same way protocol.py does: CODEX_MODEL_CATALOG
    env var, else the state-dir catalog. A missing or malformed file yields an
    empty set.
    """
    if catalog_path is None:
        catalog_path = default_catalog_path()
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


IDENTITY_LINE = (
    "You are Codex, a coding agent based on {display_name}. You and the user "
    "share one workspace, and your job is to collaborate with them until their "
    "goal is genuinely handled."
)


def model_messages(
    shared_instructions: str,
    display_name: str,
    context_window: int,
    record: dict | None = None,
) -> dict[str, Any]:
    """Build the model_messages dict Codex requires for a rendered model.

    instructions_template is computed from the shared instructions (or the
    identity line) unless the compact record provides an explicit template.
    Every other canonical model_messages key (approvals, collaboration_modes,
    permissions, token_budget, auto_review, instructions_variables) passes
    through from the record's own model_messages when present and defaults to
    null otherwise; the keys are never dropped.
    """
    source: dict = {}
    if isinstance(record, dict):
        raw = record.get("model_messages")
        if isinstance(raw, dict):
            source = raw
    explicit_template = source.get("instructions_template")
    if isinstance(explicit_template, str) and explicit_template.strip():
        template = explicit_template
    else:
        identity = IDENTITY_LINE.format(display_name=display_name)
        if not shared_instructions:
            template = identity
        else:
            template = shared_instructions.replace(shared_instructions.splitlines()[0], identity, 1)
    messages: dict[str, Any] = {"instructions_template": template}
    for key in MODEL_MESSAGES_KEYS:
        if key == "instructions_template":
            continue
        messages[key] = source.get(key, None)
    return messages


def render_full_catalog(compact: dict) -> dict:
    """Render the compact catalog to the full-shape catalog Codex consumes.

    Each compact model is projected through the canonical model shape
    (CANONICAL_MODEL_KEYS / MODEL_MESSAGES_KEYS): every key Codex reads is
    present, falling back to canonical defaults when the compact record omits
    it. base_instructions and model_messages are computed here; the input
    compact dict is not mutated.
    """
    shared_instructions = compact["shared_instructions"]
    models = []
    for record in compact.get("models", []):
        full = _canonical_model_defaults()
        for key in CANONICAL_MODEL_KEYS:
            if key in record:
                full[key] = record[key]
        slug = str(record.get("slug") or full["slug"])
        full["slug"] = slug
        full["display_name"] = str(record.get("display_name") or full["display_name"])
        full["description"] = str(record.get("description") or full["description"])
        full["base_instructions"] = shared_instructions
        context = full.get("context_window")
        if not isinstance(context, int) or context <= 0:
            context = 1000000
        limit = full.get("auto_compact_token_limit")
        full["auto_compact_token_limit"] = (
            limit if isinstance(limit, int) and limit > 0 else round(context * 0.9)
        )
        if not full.get("comp_hash"):
            full["comp_hash"] = _comp_hash_for(slug)
        full["model_messages"] = model_messages(
            shared_instructions, full["display_name"], context, record
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


def _write_json(path: str, value: dict) -> None:
    """Write JSON atomically (temp file + rename) so readers never see a partial file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".opencode-go-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_catalog(compact: dict, out_path: str) -> None:
    """Render the compact catalog and write the full-shape catalog to out_path."""
    _write_json(out_path, render_full_catalog(compact))


def parse_iso(value: str) -> datetime.datetime:
    """Parse an ISO timestamp, assuming UTC when no timezone is present."""
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _load_if_readable(path: str) -> dict | None:
    """Load a compact catalog, returning None when it is missing or invalid."""
    if not path or not os.path.exists(path):
        return None
    try:
        return load_compact(path)
    except CatalogDiscoveryError:
        return None


def state_catalog_path() -> str:
    """Path of the runtime full-shape catalog under the state dir."""
    return os.path.join(state_dir(), STATE_CATALOG_NAME)


def state_compact_path() -> str:
    """Path of the runtime compact catalog under the state dir."""
    return os.path.join(state_dir(), STATE_COMPACT_NAME)


def user_models_path() -> str:
    """Path of the user-models overlay file under the state dir."""
    return os.path.join(state_dir(), USER_MODELS_NAME)


def model_picker_path() -> str:
    """Path of the hidden-model picker state under the state dir."""
    return os.path.join(state_dir(), MODEL_PICKER_NAME)


def announced_models_path() -> str:
    """Path of the announcement first-seen state under the state dir."""
    return os.path.join(state_dir(), ANNOUNCED_MODELS_NAME)


def seed_compact_path() -> str:
    """Return the checked-in seed compact path, or "" when not installed.

    OPENCODE_GO_PROXY_SEED_CATALOG wins; otherwise the repo checkout's contrib
    file. Installed packages (uvx/uv wheels) do not ship contrib, so "" means
    the runtime starts from the state-dir compact plus discovery.
    """
    env = os.environ.get(SEED_CATALOG_ENV)
    if env:
        return env
    repo_contrib = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "contrib",
        STATE_COMPACT_NAME,
    )
    return repo_contrib if os.path.exists(repo_contrib) else ""


def load_seed_compact() -> dict | None:
    """Load the checked-in seed compact, or None when unavailable."""
    return _load_if_readable(seed_compact_path())


def load_runtime_compact() -> dict | None:
    """Load the state-dir runtime compact, or None when it does not exist yet."""
    return _load_if_readable(state_compact_path())


def read_user_models(path: str | None = None) -> list[dict]:
    """Read user-models.json entries from the state dir; [] when missing/malformed.

    File shape: {"version": 1, "models": [ {compact record fields keyed by
    slug}, ... ]}. A full record adds a model, a partial record edits it, and
    "hide": true hides it.
    """
    if path is None:
        path = user_models_path()
    try:
        with open(path) as f:
            payload = json.load(f)
        models = payload.get("models")
        if not isinstance(models, list):
            return []
        return [entry for entry in models if isinstance(entry, dict)]
    except (OSError, json.JSONDecodeError, AttributeError):
        return []


def apply_user_models(models: list[dict], user_models: list[dict]) -> list[dict]:
    """Overlay user-curated model entries onto the merged catalog.

    Each entry is keyed by slug. A full record adds a new model; a partial
    record edits display fields (display_name, description, priority,
    visibility, ...) on an existing one. "hide": true hides a model. Entries
    without a slug are ignored. Returns a new list; inputs are not mutated.
    """
    result: list[dict] = [dict(entry) for entry in models if isinstance(entry, dict)]
    index: dict[str, int] = {
        str(entry.get("slug", "")): i for i, entry in enumerate(result) if entry.get("slug")
    }
    for entry in user_models:
        if not isinstance(entry, dict):
            continue
        slug = str(entry.get("slug") or "").strip()
        if not slug:
            continue
        if slug in index:
            record = result[index[slug]]
        else:
            record = _default_model_record()
            record["slug"] = slug
            index[slug] = len(result)
            result.append(record)
        for key, value in entry.items():
            if key == "slug":
                continue
            if key == "hide":
                if value:
                    record["visibility"] = "hide"
                continue
            if key in OVERLAY_EDIT_KEYS:
                record[key] = value
    return result


def read_hidden_models(path: str | None = None) -> set[str]:
    """Read hidden-model slugs (codex-router model-picker.json shape) from state."""
    if path is None:
        path = model_picker_path()
    try:
        with open(path) as f:
            payload = json.load(f)
        hidden = payload.get("hidden")
        if not isinstance(hidden, list):
            return set()
        return {str(slug) for slug in hidden if slug}
    except (OSError, json.JSONDecodeError, AttributeError):
        return set()


def apply_hidden_models(models: list[dict], hidden: set[str]) -> list[dict]:
    """Set visibility "hide" on every model whose slug is in hidden."""
    return [
        {**entry, "visibility": "hide"} if str(entry.get("slug", "")) in hidden else entry
        for entry in models
    ]


def _format_token_count(tokens: int) -> str:
    """Compact token formatting: "1M", "400K", "64K", matching codex-router."""
    if tokens >= 995_000:
        millions = round((tokens / 1_000_000) * 10) / 10
        return f"{millions:g}M"
    return f"{round(tokens / 1000)}K"


def auto_announcement_copy(model: dict) -> str:
    """Announcement copy built only from fields already in the record."""
    details = []
    context = model.get("context_window")
    if isinstance(context, int) and context > 0:
        details.append(f"a {_format_token_count(context)}-token context window")
    efforts = [
        level.get("effort")
        for level in model.get("supported_reasoning_levels", [])
        if isinstance(level, dict) and level.get("effort")
    ]
    if len(efforts) > 1:
        details.append(f"reasoning efforts from {efforts[0]} to {efforts[-1]}")
    if "image" in (model.get("input_modalities") or []):
        details.append("image input")
    if len(details) == 1:
        capabilities = f" It comes with {details[0]}."
    elif len(details) == 2:
        capabilities = f" It comes with {details[0]} and {details[1]}."
    elif details:
        capabilities = f" It comes with {', '.join(details[:-1])}, and {details[-1]}."
    else:
        capabilities = ""
    name = model.get("display_name") or model.get("slug") or "A model"
    return f"{name} just landed in your model picker.{capabilities}"


def read_announced_at(path: str | None = None) -> dict[str, float] | None:
    """Return {slug: first-seen epoch}; None when no announcement state exists."""
    if path is None:
        path = announced_models_path()
    try:
        with open(path) as f:
            payload = json.load(f)
        models = payload.get("models")
        if not isinstance(models, dict):
            return None
        return {
            str(slug): float(seen)
            for slug, seen in models.items()
            if isinstance(seen, (int, float))
        }
    except (OSError, json.JSONDecodeError, AttributeError, ValueError):
        return None


def write_announced_at(path: str, announced_at: dict[str, float]) -> None:
    """Persist announcement first-seen state under the state dir."""
    _write_json(
        path,
        {"version": 1, "models": {slug: seen for slug, seen in sorted(announced_at.items())}},
    )


def annotate_announcements(
    models: list[dict],
    announced_at: dict[str, float] | None,
    now: float,
    curated_slugs: set[str],
) -> tuple[list[dict], dict[str, float]]:
    """Fill availability_nux for models that appeared recently and lack an announcement.

    Mirrors codex-router: the first run seeds silently (no file means an install
    never announces the whole catalog), and curated models are excluded because
    the operator added those deliberately.
    """
    first_run = announced_at is None
    next_announced = dict(announced_at or {})
    result = []
    for model in models:
        slug = str(model.get("slug", ""))
        if slug not in next_announced:
            next_announced[slug] = 0.0 if first_run else now
        if model.get("availability_nux") or slug in curated_slugs:
            result.append(model)
            continue
        seen = next_announced[slug]
        if seen == 0 or now - seen >= AUTO_ANNOUNCE_WINDOW_SECONDS:
            result.append(model)
        else:
            result.append({**model, "availability_nux": auto_announcement_copy(model)})
    return result, next_announced


def render_runtime_catalog(compact: dict) -> dict:
    """Render the full catalog from a runtime compact, applying the user overlay.

    Order: user-models overrides (add/hide/edit display), then availability
    announcements, then hidden-model flags. Announcement state is persisted
    under the state dir.
    """
    models = list(compact.get("models", []))
    user_models = read_user_models()
    curated_slugs = {str(entry.get("slug", "")) for entry in user_models}
    models = apply_user_models(models, user_models)
    announced, next_announced = annotate_announcements(
        models, read_announced_at(), time.time(), curated_slugs
    )
    write_announced_at(announced_models_path(), next_announced)
    models = apply_hidden_models(announced, read_hidden_models())
    return render_full_catalog({**compact, "models": models})


def _render_and_write(compact: dict, catalog_path: str, overlay: bool = False) -> dict:
    """Render a compact catalog (with the overlay when requested) and write it."""
    rendered = render_runtime_catalog(compact) if overlay else render_full_catalog(compact)
    _write_json(catalog_path, rendered)
    return rendered


def refresh_catalog(
    compact_path: str | None = None,
    catalog_path: str | None = None,
    seed_path: str | None = None,
    now: datetime.datetime | None = None,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    force: bool = False,
    overlay: bool = False,
) -> dict:
    """Refresh the compact catalog from models.dev when stale, then re-render.

    Default paths resolve under the state dir (OPENCODE_GO_PROXY_STATE_DIR), so
    the runtime refresh never writes the repo's checked-in contrib files. The
    checked-in seed supplies the base models when no runtime compact exists.
    overlay=True applies the user-models / announcements / hidden-model flags.

    Refreshing is skipped when the compact catalog is fresh (fetched_at within
    ttl_hours), when the OPENCODE_GO_CATALOG_REFRESH env var is "0", or when
    discovery fails and a compact catalog already exists (offline fallback).
    """
    if now is None:
        now = datetime.datetime.now(datetime.UTC)
    iso = now.isoformat()
    if compact_path is None:
        compact_path = state_compact_path()
    if catalog_path is None:
        catalog_path = state_catalog_path()
    if seed_path is None:
        seed_path = seed_compact_path()

    if os.environ.get(CATALOG_REFRESH_ENV) == "0":
        compact = load_compact(compact_path)
        return _render_and_write(compact, catalog_path, overlay=overlay)

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
                        return _render_and_write(compact, catalog_path, overlay=overlay)
                except ValueError:
                    pass

    try:
        discovered = discover_models()
    except CatalogDiscoveryError:
        if os.path.exists(compact_path):
            return _render_and_write(load_compact(compact_path), catalog_path, overlay=overlay)
        raise

    existing = _load_if_readable(compact_path)
    if existing is None:
        existing = _load_if_readable(seed_path)
    existing = existing or {}
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
    _write_json(compact_path, compact)
    return _render_and_write(compact, catalog_path, overlay=overlay)


def prepare_runtime_catalog() -> dict:
    """Render the best catalog available without network under the state dir.

    Startup fast path: uses the state-dir compact when present, else the
    checked-in seed. A background refresh_runtime_catalog() applies discovery
    and the user overlay afterwards.
    """
    compact = load_runtime_compact()
    if compact is None:
        compact = load_seed_compact()
    if compact is None:
        compact = {
            "fetched_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "etag": "",
            "shared_instructions": "",
            "client_version": "",
            "models": [],
        }
    return _render_and_write(compact, state_catalog_path(), overlay=True)


def refresh_runtime_catalog(
    *,
    now: datetime.datetime | None = None,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    force: bool = False,
) -> dict:
    """Full runtime refresh under the state dir with the user overlay applied.

    Network discovery is TTL-gated. Writes never leave the state dir; safe to
    call from a background thread at startup.
    """
    return refresh_catalog(now=now, ttl_hours=ttl_hours, force=force, overlay=True)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main_refresh(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="opencode-go-proxy refresh-catalog")
    p.add_argument("--compact", default=os.path.join(REPO_ROOT, "contrib", STATE_COMPACT_NAME))
    p.add_argument("--catalog", default=os.path.join(REPO_ROOT, "contrib", STATE_CATALOG_NAME))
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


__all__ = [
    "ANNOUNCED_MODELS_NAME",
    "AUTO_ANNOUNCE_WINDOW_SECONDS",
    "CANONICAL_MODEL_KEYS",
    "CATALOG_REFRESH_ENV",
    "DEFAULT_TTL_HOURS",
    "MODELS_DEV_URL",
    "MODEL_MESSAGES_KEYS",
    "OPTIONAL_MODEL_KEYS",
    "OVERLAY_EDIT_KEYS",
    "SEED_CATALOG_ENV",
    "STATE_CATALOG_NAME",
    "STATE_COMPACT_NAME",
    "CatalogDiscoveryError",
    "annotate_announcements",
    "apply_hidden_models",
    "apply_user_models",
    "auto_announcement_copy",
    "default_catalog_path",
    "discover_models",
    "load_compact",
    "load_known_slugs",
    "load_runtime_compact",
    "load_seed_compact",
    "main_refresh",
    "merge_models",
    "model_messages",
    "parse_iso",
    "prepare_runtime_catalog",
    "read_announced_at",
    "read_hidden_models",
    "read_user_models",
    "refresh_catalog",
    "refresh_runtime_catalog",
    "render_full_catalog",
    "render_runtime_catalog",
    "seed_compact_path",
    "state_catalog_path",
    "state_compact_path",
    "write_announced_at",
    "write_catalog",
]
