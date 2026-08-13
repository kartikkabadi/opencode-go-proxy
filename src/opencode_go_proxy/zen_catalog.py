"""Zen model catalog: capture the opencode.ai Zen model list and resolve families.

The Zen gateway (https://opencode.ai/zen/v1) serves each model through one of
four upstream surfaces:

- anthropic_messages: /zen/v1/messages with x-api-key (claude-*, qwen*)
- google_gemini: /zen/v1/models/<id> with x-goog-api-key (gemini-*)
- openai_responses: /zen/v1/responses with Authorization: Bearer (gpt-*, grok-*)
- openai_chat: /zen/v1/chat/completions with Bearer (everything else)

The model list comes from GET /zen/v1/models (no auth) and is cached under the
state dir (zen-models.json) with a fetched_at + TTL gate mirroring
catalog.refresh_catalog. Families resolve per model from models.dev metadata
(when present) with id-prefix rules as fallback, and persist to
zen-catalog.json. Reads (zen_model_ids, zen_families) are cached in memory by
file mtime, like routing.native_model_slugs, so a rewritten cache file is
picked up without a restart. Capture never raises: a fetch failure falls back
to the cached list, and with nothing cached the capture is empty.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import urllib.error
import urllib.request
from typing import Any

from . import __version__
from .meter import state_dir
from .trace import trace

Json = dict[str, Any]

ZEN_PREFIX = "zen/"

ZEN_MODELS_URL = "https://opencode.ai/zen/v1/models"
MODELS_DEV_URL = "https://models.dev/api.json"
ZEN_MODELS_NAME = "zen-models.json"
ZEN_CATALOG_NAME = "zen-catalog.json"

DEFAULT_TTL_HOURS = 24
ZEN_MODELS_REFRESH_ENV = "OPENCODE_GO_ZEN_MODELS_REFRESH"
ZEN_FETCH_TIMEOUT_SEC = 10
ZEN_FETCH_RETRIES = 1  # one retry on top of the first attempt
MODELS_DEV_TIMEOUT_SEC = 10

# models.dev family values for the opencode provider are specific (claude-opus,
# gpt-codex, gemini-flash, deepseek-flash, qwen3.5, ...): the generic
# vocabulary below maps directly, the big three classify by prefix, and
# anything else stays unmapped so the id-prefix fallback decides. qwen models,
# for example, speak the anthropic messages surface on zen even though
# models.dev groups them with the compatible crowd.
_MODELS_DEV_FAMILY_TO_ZEN = {
    "anthropic": "anthropic_messages",
    "google": "google_gemini",
    "openai": "openai_responses",
    "openai-compatible": "openai_chat",
}


class ZenModelFetchError(Exception):
    """Raised when the zen model list cannot be fetched or parsed."""


def zen_models_path() -> str:
    """Path of the raw zen model-list cache under the state dir."""
    return os.path.join(state_dir(), ZEN_MODELS_NAME)


def zen_catalog_path() -> str:
    """Path of the persisted zen family map under the state dir."""
    return os.path.join(state_dir(), ZEN_CATALOG_NAME)


def resolve_family(model_id: str, models_dev_families: dict[str, str] | None = None) -> str:
    """Zen family for a bare model id.

    Prefers the models.dev family metadata (provider "opencode") when it maps
    to a zen family; otherwise the id-prefix rules decide.
    """
    if models_dev_families:
        mapped = _zen_family_from_models_dev(models_dev_families.get(model_id))
        if mapped:
            return mapped
    return _family_from_prefix(model_id)


def _family_from_prefix(model_id: str) -> str:
    lower = model_id.lower()
    if lower.startswith(("claude", "qwen")):
        return "anthropic_messages"
    if lower.startswith("gemini"):
        return "google_gemini"
    if lower.startswith(("gpt", "grok")):
        return "openai_responses"
    return "openai_chat"


def _zen_family_from_models_dev(family: str | None) -> str | None:
    """Map a models.dev family value to a zen family; None when unmapped."""
    if not family:
        return None
    value = str(family).strip().lower()
    if value in _MODELS_DEV_FAMILY_TO_ZEN:
        return _MODELS_DEV_FAMILY_TO_ZEN[value]
    if value.startswith("claude"):
        return "anthropic_messages"
    if value.startswith("gemini"):
        return "google_gemini"
    if value.startswith(("gpt", "grok")):
        return "openai_responses"
    return None


def _parse_iso(value: str) -> datetime.datetime:
    """Parse an ISO timestamp, assuming UTC when no timezone is present."""
    parsed = datetime.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed


def _write_json(path: str, value: dict) -> None:
    """Write JSON atomically (temp file + rename) so readers never see a partial file."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".zen-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        # A leftover temp file is inert: the rename never happened, so readers
        # keep the last good capture. Log when even the cleanup fails.
        try:
            os.unlink(tmp)
        except OSError as exc:
            trace("zen_catalog.cleanup_failed", path=tmp, error=str(exc))
        raise


def _load_capture(path: str) -> dict | None:
    """Load a zen capture file; None when missing or malformed."""
    try:
        with open(path) as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        trace("zen_catalog.load_failed", path=path, error=str(exc))
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return None
    return payload


def _fetch_zen_models(timeout: int = ZEN_FETCH_TIMEOUT_SEC) -> list[dict]:
    """GET /zen/v1/models; returns the data entries; raises ZenModelFetchError.

    The endpoint needs no auth. One retry. The request identifies itself the
    way discover_models does, since models.dev rejects the default urllib
    User-Agent.
    """
    request = urllib.request.Request(
        ZEN_MODELS_URL, headers={"User-Agent": f"opencode-go-proxy/{__version__}"}
    )
    last_error: Exception | None = None
    for attempt in range(ZEN_FETCH_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                payload = json.load(resp)
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise ZenModelFetchError(f"unexpected payload shape from {ZEN_MODELS_URL}")
            return [entry for entry in data if isinstance(entry, dict)]
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
            ZenModelFetchError,
        ) as exc:
            last_error = exc
    raise ZenModelFetchError(f"failed to fetch {ZEN_MODELS_URL}: {last_error}")


def _fetch_models_dev_families(timeout: int = MODELS_DEV_TIMEOUT_SEC) -> dict[str, str]:
    """Best-effort {model_id: family} from the models.dev "opencode" provider.

    Family metadata is a refinement, never a hard dependency: a failure
    returns {} and capture falls back to the prefix rules.
    """
    request = urllib.request.Request(
        MODELS_DEV_URL, headers={"User-Agent": f"opencode-go-proxy/{__version__}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        trace("zen_catalog.models_dev_failed", error=str(exc))
        return {}
    provider = payload.get("opencode") if isinstance(payload, dict) else None
    models = provider.get("models") if isinstance(provider, dict) else None
    if not isinstance(models, dict):
        return {}
    families: dict[str, str] = {}
    for model_id, entry in models.items():
        if isinstance(entry, dict) and entry.get("family"):
            families[str(model_id)] = str(entry["family"])
    return families


def _is_fresh(capture: dict, now: datetime.datetime, ttl_hours: float) -> bool:
    """True when the capture's fetched_at is within the TTL window."""
    fetched_at = capture.get("fetched_at")
    if not isinstance(fetched_at, str):
        return False
    try:
        fetched = _parse_iso(fetched_at)
    except ValueError:
        return False
    return now - fetched < datetime.timedelta(hours=ttl_hours)


def _resolve_families(model_ids: list[str], models_dev_families: dict[str, str]) -> dict[str, str]:
    return {model_id: resolve_family(model_id, models_dev_families) for model_id in model_ids}


def capture_zen_models(
    now: datetime.datetime | None = None,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    force: bool = False,
) -> dict:
    """Refresh the zen model capture when stale; never raises on network failure.

    Mirrors catalog.refresh_catalog's TTL pattern: a cache whose fetched_at is
    within ttl_hours skips the network, and OPENCODE_GO_ZEN_MODELS_REFRESH=0
    disables it entirely. A successful fetch persists the raw list to
    zen-models.json and the resolved families to zen-catalog.json. On fetch
    failure the cached capture is returned when present, else an empty models
    list. Returns {"fetched_at": iso, "models": [entry, ...]}.
    """
    if now is None:
        now = datetime.datetime.now(datetime.UTC)
    iso = now.isoformat()
    models_path = zen_models_path()

    if os.environ.get(ZEN_MODELS_REFRESH_ENV) == "0":
        cached = _load_capture(models_path)
        return cached if cached is not None else {"fetched_at": iso, "models": []}

    if not force:
        cached = _load_capture(models_path)
        if cached is not None and _is_fresh(cached, now, ttl_hours):
            return cached

    try:
        entries = _fetch_zen_models()
    except ZenModelFetchError as exc:
        trace("zen_catalog.fetch_failed", error=str(exc))
        cached = _load_capture(models_path)
        return cached if cached is not None else {"fetched_at": iso, "models": []}

    model_ids = [str(entry["id"]) for entry in entries if isinstance(entry, dict) and entry.get("id")]
    models_dev_families = _fetch_models_dev_families() if model_ids else {}
    families = _resolve_families(model_ids, models_dev_families)
    _write_json(models_path, {"fetched_at": iso, "models": entries})
    _write_json(
        zen_catalog_path(),
        {
            "version": 1,
            "fetched_at": iso,
            "models": {model_id: {"family": families[model_id]} for model_id in sorted(families)},
        },
    )
    return {"fetched_at": iso, "models": entries}


_ZEN_MODELS_CACHE: tuple[str, int | None, frozenset[str]] | None = None


def zen_model_ids() -> set[str]:
    """Bare zen model ids from the capture cache, refreshed by file mtime.

    An empty set when the capture has not run or is unreadable.
    """
    global _ZEN_MODELS_CACHE
    path = zen_models_path()
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        mtime = None
    if (
        _ZEN_MODELS_CACHE is not None
        and _ZEN_MODELS_CACHE[0] == path
        and _ZEN_MODELS_CACHE[1] == mtime
    ):
        return set(_ZEN_MODELS_CACHE[2])
    model_ids: frozenset[str] = frozenset()
    if mtime is not None:
        capture = _load_capture(path)
        model_ids = frozenset(
            str(entry["id"]) for entry in capture["models"] if isinstance(entry, dict) and entry.get("id")
        )
    _ZEN_MODELS_CACHE = (path, mtime, model_ids)
    return set(model_ids)


_ZEN_FAMILIES_CACHE: tuple[str, int | None, str, int | None, dict[str, str]] | None = None


def zen_families() -> dict[str, str]:
    """Bare zen model id -> zen family, refreshed by file mtime.

    Persisted families from zen-catalog.json win. Ids the capture knows but
    the persisted map misses resolve on the fly with the prefix rules, so the
    two cache files can drift (for example zen-catalog.json deleted) without
    leaving holes.
    """
    global _ZEN_FAMILIES_CACHE
    catalog_path = zen_catalog_path()
    models_path = zen_models_path()
    catalog_mtime = _file_mtime(catalog_path)
    models_mtime = _file_mtime(models_path)
    if (
        _ZEN_FAMILIES_CACHE is not None
        and _ZEN_FAMILIES_CACHE[0] == catalog_path
        and _ZEN_FAMILIES_CACHE[1] == catalog_mtime
        and _ZEN_FAMILIES_CACHE[2] == models_path
        and _ZEN_FAMILIES_CACHE[3] == models_mtime
    ):
        return dict(_ZEN_FAMILIES_CACHE[4])
    families = _read_persisted_families(catalog_path)
    for model_id in sorted(zen_model_ids() - set(families)):
        families[model_id] = _family_from_prefix(model_id)
    _ZEN_FAMILIES_CACHE = (catalog_path, catalog_mtime, models_path, models_mtime, families)
    return dict(families)


def _file_mtime(path: str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _read_persisted_families(path: str) -> dict[str, str]:
    """{id: family} from zen-catalog.json; {} when missing or malformed."""
    try:
        with open(path) as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        trace("zen_catalog.read_failed", path=path, error=str(exc))
        return {}
    raw = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    families: dict[str, str] = {}
    for model_id, meta in raw.items():
        if isinstance(meta, dict) and meta.get("family"):
            families[str(model_id)] = str(meta["family"])
    return families


__all__ = [
    "DEFAULT_TTL_HOURS",
    "MODELS_DEV_URL",
    "ZEN_MODELS_NAME",
    "ZEN_MODELS_REFRESH_ENV",
    "ZEN_MODELS_URL",
    "ZEN_PREFIX",
    "ZenModelFetchError",
    "capture_zen_models",
    "resolve_family",
    "zen_catalog_path",
    "zen_families",
    "zen_model_ids",
    "zen_models_path",
]
