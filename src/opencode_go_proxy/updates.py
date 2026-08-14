"""GitHub release check with a daily TTL cache (update support plan).

GET /version, /state, and the CLI update command all read this module:
check_for_updates() fetches the latest published release from GitHub and
caches the result under the state dir, so every surface sees one consistent
answer without hammering the API. Every failure degrades to the error shape
instead of raising, so a proxy that is offline still serves /version and
/state with the last good answer.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from . import __version__
from .meter import state_dir
from .trace import trace

Json = dict[str, Any]

# GitHub API base; OPENCODE_GO_PROXY_GITHUB_API overrides it (tests and
# mirrors point the check at a local fixture).
GITHUB_API_ENV = "OPENCODE_GO_PROXY_GITHUB_API"
DEFAULT_GITHUB_API = "https://api.github.com"
RELEASES_LATEST_PATH = "/repos/kartikkabadi/opencode-go-proxy/releases/latest"

# How long a cached check stays fresh; OPENCODE_GO_PROXY_UPDATE_TTL_HOURS
# overrides the default (0 disables the cache so every call re-checks).
TTL_HOURS_ENV = "OPENCODE_GO_PROXY_UPDATE_TTL_HOURS"
DEFAULT_TTL_HOURS = 24
DEFAULT_TTL_SECONDS = DEFAULT_TTL_HOURS * 3600

CACHE_FILENAME = "update-check.json"
# The cache holds no secrets, but the state dir is the user's own data dir;
# 0600 matches quota-state.json's convention of never being group-readable.
CACHE_MODE = 0o600

USER_AGENT = f"opencode-go-proxy/{__version__}"

# updates.py lives at <repo>/src/opencode_go_proxy/updates.py; the git work
# tree root is three levels up.
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


@dataclass
class UpdateInfo:
    """One release check: the version in use, the newest published release,
    and whether that release is newer (prereleases never count)."""

    current: str
    latest: str | None = None
    available: bool = False
    release_url: str | None = None
    checked_at: str | None = None
    error: str | None = None


def _cache_path() -> str:
    return os.path.join(state_dir(), CACHE_FILENAME)


def _now_iso() -> str:
    """UTC ISO-8601 with a Z suffix, matching the repo's timestamp style."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ttl_seconds() -> int:
    """Freshness window for a cached check, from env or the 24h default."""
    raw = os.environ.get(TTL_HOURS_ENV)
    if raw is None:
        return DEFAULT_TTL_SECONDS
    try:
        hours = float(raw)
    except ValueError:
        return DEFAULT_TTL_SECONDS
    if hours <= 0:
        return 0
    return int(hours * 3600)


def _tag_without_v(tag: Any) -> str | None:
    """GitHub tag name without the leading v ("v0.4.4" -> "0.4.4")."""
    if not isinstance(tag, str) or not tag:
        return None
    return tag[1:] if tag[0] in {"v", "V"} else tag


def _version_key(version: str | None) -> tuple[int, ...] | None:
    """Numeric core of a semver-ish string, or None when unparseable.

    Strips a leading v and drops build metadata (+...) and prerelease
    suffixes (-...), so "v1.2.3-rc.1+build.5" compares as (1, 2, 3).
    """
    if not version:
        return None
    core = version.strip().lstrip("vV").split("+", 1)[0].split("-", 1)[0]
    parts: list[int] = []
    for part in core.split("."):
        if not part.isdigit():
            return None
        parts.append(int(part))
    return tuple(parts)


def _is_prerelease(version: str | None) -> bool:
    """True for tags carrying a prerelease or dev suffix (v1.2.3-rc.1)."""
    if not version:
        return False
    core = version.strip().lstrip("vV").split("+", 1)[0]
    return "-" in core


def _is_newer(latest: str | None, current: str) -> bool:
    """True when latest is a non-prerelease release newer than current."""
    if latest is None or _is_prerelease(latest):
        return False
    latest_key = _version_key(latest)
    current_key = _version_key(current)
    if latest_key is None or current_key is None:
        return False
    return latest_key > current_key


def _fetch_latest_release(timeout: int) -> Json:
    """GET /releases/latest; returns the release object or raises.

    The endpoint answers a single release object; a list (some mirrors and
    test fixtures) is tolerated by taking the first entry.
    """
    base = (os.environ.get(GITHUB_API_ENV) or DEFAULT_GITHUB_API).rstrip("/")
    request = urllib.request.Request(
        f"{base}{RELEASES_LATEST_PATH}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        payload = json.load(resp)
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        raise TypeError(f"unexpected GitHub response shape: {type(payload).__name__}")
    return payload


def _read_cache() -> Json | None:
    """Cached check payload, or None when missing or unreadable."""
    try:
        with open(_cache_path(), encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _cache_age_seconds() -> float | None:
    """Seconds since the cache was written; None when it does not exist."""
    try:
        return time.time() - os.path.getmtime(_cache_path())
    except OSError:
        return None


def _write_cache(payload: Json) -> None:
    """Persist a check result with mode 0600; a failed write is traced, never fatal."""
    try:
        fd = os.open(_cache_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, CACHE_MODE)
    except OSError as exc:
        trace("updates.cache_write_failed", error=str(exc))
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
    except OSError as exc:
        trace("updates.cache_write_failed", error=str(exc))


def _info_from_cache(current: str, cached: Json) -> UpdateInfo:
    """Rebuild the UpdateInfo from a cached payload (types re-validated)."""
    latest = cached.get("latest")
    latest_str = latest if isinstance(latest, str) else None
    release_url = cached.get("release_url")
    checked_at = cached.get("checked_at")
    error = cached.get("error")
    return UpdateInfo(
        current=current,
        latest=latest_str,
        available=_is_newer(latest_str, current),
        release_url=release_url if isinstance(release_url, str) else None,
        checked_at=checked_at if isinstance(checked_at, str) else None,
        error=error if isinstance(error, str) else None,
    )


def _info_from_error(current: str, cached: Json | None, exc: Exception) -> UpdateInfo:
    """Degraded result for a failed live check: error set, last good values kept."""
    latest = cached.get("latest") if cached else None
    latest_str = latest if isinstance(latest, str) else None
    release_url = cached.get("release_url") if cached else None
    checked_at = _now_iso()
    info = UpdateInfo(
        current=current,
        latest=latest_str,
        available=_is_newer(latest_str, current),
        release_url=release_url if isinstance(release_url, str) else None,
        checked_at=checked_at,
        error=f"{type(exc).__name__}: {exc}",
    )
    # Persist the failure so an offline /state serves the error shape from
    # cache instead of blocking on a timed-out fetch on every poll.
    _write_cache({
        "checked_at": checked_at,
        "latest": latest_str,
        "release_url": info.release_url,
        "error": info.error,
    })
    return info


def check_for_updates(force: bool = False, timeout: int = 8) -> UpdateInfo:
    """Latest published release, daily-TTL cached under the state dir.

    force=True bypasses the cache. Offline and HTTP failures degrade to the
    error shape and keep the previous successful values when one is cached,
    so /version and /state always get a stable update block.
    """
    current = __version__
    cached = _read_cache()
    age = _cache_age_seconds()
    if cached is not None and age is not None and not force and age < _ttl_seconds():
        return _info_from_cache(current, cached)

    try:
        release = _fetch_latest_release(timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        return _info_from_error(current, cached, exc)

    info = UpdateInfo(
        current=current,
        latest=_tag_without_v(release.get("tag_name")),
        release_url=release.get("html_url") if isinstance(release.get("html_url"), str) else None,
        checked_at=_now_iso(),
        error=None,
    )
    info.available = _is_newer(info.latest, current)
    _write_cache({
        "checked_at": info.checked_at,
        "latest": info.latest,
        "release_url": info.release_url,
        "error": None,
    })
    return info


@lru_cache(maxsize=1)
def _git_commit() -> str | None:
    """Short HEAD hash of the checkout, or None outside a git work tree."""
    try:
        result = subprocess.run(
            ["git", "-C", _REPO_ROOT, "rev-parse", "--short=8", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        trace("updates.git_commit_failed", error=str(exc))
        return None
    if result.returncode != 0:
        return None
    commit = result.stdout.strip()
    return commit or None


def version_payload(force: bool = False) -> Json:
    """The /version document: package version, git commit, update block."""
    info = check_for_updates(force=force)
    return {
        "version": __version__,
        "git_commit": _git_commit(),
        "update": {
            "available": info.available,
            "checked_at": info.checked_at,
            "error": info.error,
            "latest": info.latest,
            "release_url": info.release_url,
        },
    }
