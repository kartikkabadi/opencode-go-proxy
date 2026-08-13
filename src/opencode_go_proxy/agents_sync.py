"""Sync ~/.codex/agents TOMLs for opencode-go catalog models.

Writes one agent TOML per opencode-go model (name router_opencode_go_<slug>,
model_provider "opencode-go", model "opencode-go/<slug>") under ~/.codex/agents
and removes agent TOMLs this proxy previously wrote whose model left the
catalog. Files owned by other tools (for example codex-router's
router-model-* files) carry no ownership marker and are left untouched.

The opencode-go slug set comes from the compact catalog (state-dir compact,
else the checked-in seed), the same source the merge pipeline renders.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile

from . import catalog

AGENTS_DIR_ENV = "OPENCODE_GO_PROXY_AGENTS_DIR"
DEFAULT_AGENTS_DIR = os.path.join(os.path.expanduser("~"), ".codex", "agents")

OWNERSHIP_MARKER = "# managed by opencode-go-proxy"
AGENT_FILE_PREFIX = "router-opencode-go-"
AGENT_FILE_SUFFIX = ".toml"


def agents_dir() -> str:
    """The ~/.codex/agents directory the sync operates on (env override for tests)."""
    return os.environ.get(AGENTS_DIR_ENV) or DEFAULT_AGENTS_DIR


def _opencode_go_models() -> list[tuple[str, str]] | None:
    """(slug, display_name) for opencode-go catalog models, or None when the catalog is unreadable."""
    compact = catalog.load_runtime_compact()
    if compact is None:
        compact = catalog.load_seed_compact()
    if compact is None:
        return None
    models: list[tuple[str, str]] = []
    for entry in compact.get("models", []):
        if isinstance(entry, dict) and entry.get("slug"):
            slug = str(entry["slug"])
            models.append((slug, str(entry.get("display_name") or slug)))
    return sorted(models)


def agent_file_name(slug: str) -> str:
    return f"{AGENT_FILE_PREFIX}{slug}{AGENT_FILE_SUFFIX}"


def _agent_contents(slug: str, display_name: str) -> str:
    name = f"router_opencode_go_{slug}"
    return (
        f"{OWNERSHIP_MARKER}; refresh with: opencode-go-proxy agents-sync\n"
        f"name = {json.dumps(name)}\n"
        f"description = {json.dumps(f'{display_name} (opencode-go) routed through the local opencode-go-proxy.')}\n"
        'model_provider = "opencode-go"\n'
        f"model = {json.dumps(f'opencode-go/{slug}')}\n"
    )


def _atomic_write(path: str, contents: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(contents)
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def sync_agents() -> dict[str, object]:
    """Write agent TOMLs for current opencode-go models; drop stale owned ones.

    Returns {"path", "written", "unchanged", "removed", "collisions",
    "catalog"}. A target file that exists without the ownership marker is
    never overwritten — it is reported under "collisions" and left untouched.
    When the catalog is unreadable nothing is written or removed, so a failed
    refresh can never wipe the agent set.
    """
    directory = agents_dir()
    models = _opencode_go_models()
    if models is None:
        return {
            "path": directory,
            "written": [],
            "unchanged": [],
            "removed": [],
            "collisions": [],
            "catalog": "missing",
        }
    targets = {agent_file_name(slug): _agent_contents(slug, display) for slug, display in models}
    written: list[str] = []
    collisions: list[str] = []
    unchanged: list[str] = []
    removed: list[str] = []
    if os.path.isdir(directory):
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    existing = handle.read()
            except OSError:
                continue
            if name in targets:
                if existing == targets[name]:
                    unchanged.append(name)
                elif OWNERSHIP_MARKER in existing:
                    _atomic_write(path, targets[name])
                    written.append(name)
                else:
                    collisions.append(name)
            elif OWNERSHIP_MARKER in existing:
                try:
                    os.unlink(path)
                    removed.append(name)
                except OSError:
                    continue
    for name in sorted(set(targets) - set(written) - set(unchanged) - set(collisions)):
        path = os.path.join(directory, name)
        _atomic_write(path, targets[name])
        written.append(name)
    return {
        "path": directory,
        "written": written,
        "unchanged": unchanged,
        "removed": removed,
        "collisions": collisions,
        "catalog": "ok",
    }


def agents_sync_cmd(argv: list[str] | None = None) -> int:
    """opencode-go-proxy agents-sync: sync agent TOMLs to the opencode-go catalog."""
    args = list(argv or [])
    as_json = "--json" in args
    report = sync_agents()
    if as_json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        if report["catalog"] == "missing":
            sys.stdout.write("agents-sync: catalog unavailable; nothing changed\n")
        else:
            sys.stdout.write(
                f"agents-sync: {len(report['written'])} written, "
                f"{len(report['unchanged'])} unchanged, {len(report['removed'])} removed\n"
            )
            for name in report["written"]:
                sys.stdout.write(f"  wrote {name}\n")
            for name in report["removed"]:
                sys.stdout.write(f"  removed {name}\n")
    return 0
