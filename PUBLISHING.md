# Publishing

This repository is published on GitHub as
`kartikkabadi/opencode-go-proxy`.

## Release surface

- Package: `opencode-go-proxy`
- Current version: `0.2.0`
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

1. Bump version in `pyproject.toml`, `src/opencode_go_proxy/__init__.py`, this file.
2. Add `CHANGELOG.md` entry.
3. Commit, tag `vX.Y.Z`, push.
4. CI builds the wheel and creates the GitHub release automatically.

## License

MIT. See [LICENSE](LICENSE).
