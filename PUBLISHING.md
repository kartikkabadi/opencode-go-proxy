# Publishing

This repository is published on GitHub as
`kartikkabadi/opencode-go-proxy`.

## Release surface

- Package: `opencode-go-proxy`
- Current version: `0.4.10`
- CLI entry point: `opencode-go-proxy`
- Python: `>=3.11`
- Build backend: `uv_build`
- Verification: `uv run python -m pytest tests -v`,
  `uvx ruff check`, `uv build`

## Install

```bash
uvx --from git+https://github.com/kartikkabadi/opencode-go-proxy opencode-go-proxy
```

No PyPI, no AUR. `uvx` from git is the only install path.

## Release flow

1. Run `python scripts/make-release.py <version>` from the repo root (in
   `.venv` or `uv run`). It asserts a clean tree on `main`, bumps
   `pyproject.toml` and `src/opencode_go_proxy/__init__.py`, writes the
   `CHANGELOG.md` section from commits since the last tag, updates the
   "Current version" line here, commits `release: v<version>`, tags
   `v<version>`, and pushes both. Use `--dry-run` first to preview the
   whole plan without writing anything.
2. The tag push triggers CI (`ci.yml`): the `test` job runs the suite,
   `uv build` produces the wheel, and the `release` job attaches
   `dist/*.whl` to a GitHub release with auto-generated notes
   (`softprops/action-gh-release`). No other distribution step exists.

## License

MIT. See [LICENSE](LICENSE).
