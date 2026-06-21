# OpenCode Go Proxy

[![CI](https://github.com/kartikkabadi/opencode-go-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/kartikkabadi/opencode-go-proxy/actions/workflows/ci.yml)

Use your [OpenCode Go](https://opencode.ai) subscription in the [Codex app](https://github.com/openai/codex).

Codex expects a Responses API (`/v1/responses`). OpenCode Go exposes an OpenAI-compatible
Chat Completions API (`/v1/chat/completions`). This proxy bridges that gap in one local process:

```text
Codex /v1/responses
  -> opencode-go-proxy (localhost:8787)
      -> OpenCode Go /v1/chat/completions
```

## Why

Codex is a great coding agent. OpenCode Go gives you access to models like DeepSeek V4 Pro
and Flash at competitive prices. Without this proxy, you can't use them together — Codex
rejects `wire_api = "chat"` and requires Responses-shaped providers.

## Quick start

```bash
# Install and run
uvx --from git+https://github.com/kartikkabadi/opencode-go-proxy \
  opencode-go-proxy \
  --bind 127.0.0.1 \
  --port 8787

# Point Codex at it (~/.codex/config.toml)
```

```toml
[model_providers.opencode-go]
name = "OpenCode Go"
base_url = "http://127.0.0.1:8787/v1"
experimental_bearer_token = "any-string-here"
wire_api = "responses"

[profiles.deepseek-v4-pro]
model_provider = "opencode-go"
model = "deepseek-v4-pro"
model_context_window = 1000000
approval_policy = "untrusted"
sandbox_mode = "workspace-write"
features = { memories = false }

[profiles.deepseek-v4-flash]
model_provider = "opencode-go"
model = "deepseek-v4-flash"
model_context_window = 1000000
approval_policy = "untrusted"
sandbox_mode = "workspace-write"
features = { memories = false }
```

```bash
# Start Codex with a profile
codex -p deepseek-v4-pro
codex -p deepseek-v4-flash
```

## API key

The proxy resolves your OpenCode Go API key in this order:

1. `$OPENCODE_GO_API_KEY` environment variable
2. macOS keychain entry `opencode-go-api-key` (override with `CODEX_KEYCHAIN_SERVICE`)

```bash
# Option 1: env var
export OPENCODE_GO_API_KEY="your-key-here"

# Option 2: macOS keychain
security add-generic-password -a "$USER" -s opencode-go-api-key -w
```

## Recommended: lazycodex

[lazycodex](https://github.com/code-yeongyu/oh-my-openagent) is a Codex plugin that adds
multi-model orchestration, parallel background agents, and LSP/AST-aware tools. It pairs
naturally with this proxy — you get OpenCode Go's models as the backend and lazycodex's
agent harness on top.

```bash
npm install -g lazycodex-ai
```

See the [lazycodex docs](https://github.com/code-yeongyu/oh-my-openagent) for setup.

## Features

- Responses `input` to chat `messages` translation
- `instructions` and `developer` roles mapped to system messages
- Function tool schema passthrough
- Custom/freeform tool adaptation (Codex `apply_patch` works)
- Reasoning content replay across tool-call turns
- Real-time SSE streaming (not synthesized)
- Image captioning via MiMo sub-call when tools are present
- SSRF protection on image URLs (`data:image/` and `https://` only)
- Configurable body cap, bind address guard, keychain credential resolution
- Local health and model-list endpoints

## Install

### From source (no package manager)

```bash
uvx --from git+https://github.com/kartikkabadi/opencode-go-proxy opencode-go-proxy --help
```

### From a development checkout

```bash
uv sync
uv run opencode-go-proxy --help
```

### Arch Linux (AUR)

```bash
yay -S opencode-go-proxy-git
systemctl --user enable --now opencode-go-proxy.service
```

### macOS (launchd)

A launchd plist can keep the proxy running in the background. See
`contrib/` for example service files. The proxy is designed for launchd's
`KeepAlive` — it restarts on crash and starts at login.

## Configuration

All flags have environment variable defaults:

| Flag | Env var | Default |
|------|---------|---------|
| `--bind` | `OPENCODE_GO_PROXY_BIND` | `127.0.0.1` |
| `--port` | `OPENCODE_GO_PROXY_PORT` | `8787` |
| `--chat-base-url` | `CHAT_COMPLETIONS_BASE_URL` | `https://opencode.ai/zen/go/v1` |
| `--api-key-env` | `OPENCODE_GO_PROXY_API_KEY_ENV` | `OPENCODE_GO_API_KEY` |
| `--timeout-sec` | `OPENCODE_GO_PROXY_TIMEOUT_SEC` | `180` |
| `--max-body-mb` | `OPENCODE_GO_PROXY_MAX_BODY_MB` | `20` |

The proxy accepts both `/responses` and `/v1/responses`.

## Model catalog

Without a catalog entry, Codex prints a model metadata warning every turn. To suppress it,
set `model_catalog_json` in your Codex config and add entries for the models you use:

```toml
model_catalog_json = "/home/you/.codex/model-catalog.json"
```

The catalog JSON shape:

```json
{
  "models": [
    {
      "slug": "deepseek-v4-pro",
      "display_name": "DeepSeek V4 Pro",
      "context_window": 1000000,
      "max_context_window": 1000000,
      "supported_in_api": true,
      "base_instructions": "..."
    }
  ]
}
```

## Trace

Every request emits compact JSON lines on stderr. Important events:

- `server.start`
- `request.received`
- `request.converted`
- `credential.source`
- `upstream.start`
- `upstream.done`
- `response.converted`
- `request.failed`

## Development

```bash
uv run python -m pytest tests -v
uvx ruff check
uv build
```

## License

MIT. See [LICENSE](LICENSE).
