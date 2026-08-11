"""CLI for curating custom models via the user-models.json overlay.

``opencode-go-proxy models list|add|remove|hide|show`` writes the same
user-models.json shape catalog.apply_user_models() reads, so custom models
are manageable without hand-editing JSON. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any

from . import catalog

JSON_DOC = "https://github.com/kartikkabadi/opencode-go-proxy"  # placeholder


def _overlay_path() -> str:
    return catalog.user_models_path()


def _load(path: str) -> dict[str, Any]:
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, AttributeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    models = data.get("models")
    if not isinstance(models, list):
        models = []
    return {"version": 1, "models": [m for m in models if isinstance(m, dict)]}


def _save(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".user-models-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _find(models: list[dict], slug: str) -> int:
    for i, entry in enumerate(models):
        if str(entry.get("slug") or "").strip() == slug:
            return i
    return -1


def _cmd_list(argv: list[str]) -> int:
    data = _load(_overlay_path())
    models = data["models"]
    if not models:
        print("no user-curated models")
        return 0
    for entry in sorted(models, key=lambda m: str(m.get("slug") or "")):
        slug = entry.get("slug", "")
        name = entry.get("display_name") or slug
        visibility = entry.get("visibility", "list")
        if entry.get("hide"):
            visibility = "hide"
        print(f"{slug}\t{name}\t{visibility}")
    return 0


def _cmd_add(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="models add")
    parser.add_argument("slug")
    parser.add_argument("--display-name")
    parser.add_argument("--description")
    parser.add_argument("--hide", action="store_true")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    slug = args.slug.strip()
    if not slug:
        print("error: slug must not be empty", file=sys.stderr)
        return 2
    entry: dict[str, Any] = {"slug": slug}
    if args.display_name:
        entry["display_name"] = args.display_name
    if args.description:
        entry["description"] = args.description
    if args.hide:
        entry["hide"] = True
    for kv in args.set:
        if "=" not in kv:
            print(f"error: --set expects KEY=VALUE, got {kv!r}", file=sys.stderr)
            return 2
        key, value = kv.split("=", 1)
        key = key.strip()
        if key == "slug" or key == "hide":
            print(f"error: use dedicated flags for {key!r}", file=sys.stderr)
            return 2
        if key not in catalog.OVERLAY_EDIT_KEYS:
            print(f"error: unknown overlay key {key!r}", file=sys.stderr)
            return 2
        entry[key] = value
    path = _overlay_path()
    data = _load(path)
    idx = _find(data["models"], slug)
    if idx >= 0 and not args.force:
        print(f"error: model {slug!r} already curated (use --force to replace)", file=sys.stderr)
        return 2
    if idx >= 0:
        data["models"][idx] = entry
    else:
        data["models"].append(entry)
    _save(path, data)
    print(f"added {slug}")
    return 0


def _cmd_remove(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="models remove")
    parser.add_argument("slug")
    args = parser.parse_args(argv)
    path = _overlay_path()
    data = _load(path)
    idx = _find(data["models"], args.slug)
    if idx < 0:
        print(f"model {args.slug!r} is not curated")
        return 0
    del data["models"][idx]
    _save(path, data)
    print(f"removed {args.slug}")
    return 0


def _cmd_visibility(argv: list[str], hidden: bool) -> int:
    parser = argparse.ArgumentParser(prog="models hide|show")
    parser.add_argument("slug")
    args = parser.parse_args(argv)
    path = _overlay_path()
    data = _load(path)
    idx = _find(data["models"], args.slug)
    entry = {"slug": args.slug}
    if hidden:
        entry["hide"] = True
    else:
        entry["visibility"] = "list"
    if idx >= 0:
        data["models"][idx] = entry
    else:
        data["models"].append(entry)
    _save(path, data)
    print(f"{'hidden' if hidden else 'shown'} {args.slug}")
    return 0


def models_cmd(argv: list[str] | None = None) -> int:
    args = list(sys.argv[2:] if argv is None else argv)
    if not args:
        print("usage: opencode-go-proxy models list|add|remove|hide|show ...", file=sys.stderr)
        return 2
    sub, rest = args[0], args[1:]
    if sub == "list":
        return _cmd_list(rest)
    if sub == "add":
        return _cmd_add(rest)
    if sub == "remove":
        return _cmd_remove(rest)
    if sub == "hide":
        return _cmd_visibility(rest, hidden=True)
    if sub == "show":
        return _cmd_visibility(rest, hidden=False)
    print(f"unknown models subcommand {sub!r}", file=sys.stderr)
    return 2
