# Contributing

## Setup

```bash
git clone https://github.com/kartikkabadi/opencode-go-proxy.git
cd opencode-go-proxy
uv sync
```

## Development

```bash
# Run tests
uv run python -m pytest tests -v

# Lint
uvx ruff check

# Build
uv build
```

## Code style

- Python 3.11+, stdlib only (no dependencies).
- Compact code. Delete before adding. One line before fifty.
- Match existing style in `app.py` and `protocol.py`.
- Trace every network call with the `trace()` function.

## Pull requests

1. Fork and branch from `main`.
2. Write tests for new behavior.
3. Ensure `pytest tests -v` and `ruff check` pass.
4. Keep diffs minimal — surgical changes only.
5. Reference issues in your PR description.

## Reporting issues

Include:
- Codex version (`codex --version`)
- Proxy version (`opencode-go-proxy --help` shows it in the build)
- Upstream provider (OpenCode Go, DeepSeek, etc.)
- Trace output (stderr JSON lines — redact API keys first)
- Minimal repro steps

## Security

See [SECURITY.md](SECURITY.md). Do not open public issues for security vulnerabilities.
