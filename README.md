# OpenCode Go Proxy

[![CI](https://github.com/kartikkabadi/opencode-go-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/kartikkabadi/opencode-go-proxy/actions/workflows/ci.yml)

Use your [OpenCode Go](https://opencode.ai/docs/go) subscription in the [Codex app](https://github.com/openai/codex).

Codex expects a Responses API (`/v1/responses`). OpenCode Go exposes an OpenAI-compatible
Chat Completions API (`/v1/chat/completions`). This proxy bridges that gap in one local process:

```text
Codex /v1/responses
  -> opencode-go-proxy (localhost:8787)
      -> OpenCode Go /v1/chat/completions
```

## Why

OpenCode Go is $5 for the first month, then $10/month. You get access to 13 open coding models
hosted in the US, EU, and Singapore. Codex is a great agent but doesn't speak Chat Completions
natively — it requires Responses-shaped providers. This proxy fixes that.

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
codex -p deepseek-v4-flash
```

## Available models

All 13 OpenCode Go models work through this proxy. The defaults are DeepSeek V4 Flash
(cheapest general-purpose) and MiMo V2.5 (cheapest vision, used for image captioning).
Switch to whatever you want — just change the model in your Codex profile.

| Model | Slug | Best for | Requests/mo on Go |
|-------|------|----------|-------------------|
| DeepSeek V4 Flash | `deepseek-v4-flash` | Everyday coding (default) | ~158k |
| DeepSeek V4 Pro | `deepseek-v4-pro` | Complex reasoning | ~17k |
| MiMo V2.5 | `mimo-v2.5` | Vision/image captioning (default) | ~150k |
| MiMo V2.5 Pro | `mimo-v2.5-pro` | Vision + reasoning | ~16k |
| GLM-5.2 | `glm-5.2` | Frontier open model | ~4.3k |
| GLM-5.1 | `glm-5.1` | Previous-gen GLM | ~4.3k |
| Kimi K2.7 Code | `kimi-k2.7-code` | Code-specialized | ~9.3k |
| Kimi K2.6 | `kimi-k2.6` | General-purpose | ~5.8k |
| MiniMax M3 | `minimax-m3` | MiniMax flagship | ~16k |
| MiniMax M2.7 | `minimax-m2.7` | Previous-gen MiniMax | ~17k |
| Qwen3.7 Max | `qwen3.7-max` | Strong reasoning | ~4.8k |
| Qwen3.7 Plus | `qwen3.7-plus` | Mid-tier value | ~22k |
| Qwen3.6 Plus | `qwen3.6-plus` | Previous-gen Qwen | ~16k |

Request counts are estimates from [OpenCode Go docs](https://opencode.ai/docs/go) based on
typical usage patterns. Cheaper models = more requests per month.

### Switching models

Just create another profile and use `codex -p <profile-name>`:

```toml
[profiles.deepseek-v4-pro]
model_provider = "opencode-go"
model = "deepseek-v4-pro"
model_context_window = 1000000
approval_policy = "untrusted"
sandbox_mode = "workspace-write"
features = { memories = false }

[profiles.glm-5.2]
model_provider = "opencode-go"
model = "glm-5.2"
model_context_window = 272000
approval_policy = "untrusted"
sandbox_mode = "workspace-write"
features = { memories = false }

[profiles.kimi-k2.7-code]
model_provider = "opencode-go"
model = "kimi-k2.7-code"
model_context_window = 272000
approval_policy = "untrusted"
sandbox_mode = "workspace-write"
features = { memories = false }
```

```bash
codex -p deepseek-v4-pro
codex -p glm-5.2
codex -p kimi-k2.7-code
```

### How the default model is chosen

The proxy picks the upstream model based on what Codex sends:

1. If the model slug is in the [alias map](src/opencode_go_proxy/protocol.py), it's mapped
   (e.g. `gpt-5.5` → `deepseek-v4-pro`).
2. If the model slug is a known OpenCode Go model (from the catalog), it's used as-is.
3. Otherwise, it falls back to `deepseek-v4-flash`.

When images are present in a turn with tools, the proxy routes to MiMo V2.5 for image
captioning (it's the cheapest vision model on Go), then routes the main turn to your
configured model. Override the vision model with `CODEX_IMAGE_MODEL`.

## API key

The proxy resolves your OpenCode Go API key in this order:

1. `$OPENCODE_GO_API_KEY` environment variable
2. macOS keychain entry `opencode-go-api-key` (override with `CODEC_KEYCHAIN_SERVICE`)

```bash
# Option 1: env var
export OPENCODE_GO_API_KEY="your-key-here"

# Option 2: macOS keychain
security add-generic-password -a "$USER" -s opencode-go-api-key -w
```

Get your API key from [OpenCode Zen](https://opencode.ai/zen) after subscribing to Go.

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
- Image captioning via MiMo V2.5 when tools are present (override with `CODEX_IMAGE_MODEL`)
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

Without a catalog entry, Codex prints a model metadata warning every turn. A reference catalog
with all OpenCode Go models is included at `contrib/opencode-go-catalog.json`. Copy it and
point Codex at it:

```bash
cp contrib/opencode-go-catalog.json ~/.codex/model-catalog.json
```

```toml
model_catalog_json = "/home/you/.codex/model-catalog.json"
```

The catalog has minimal fields (slug, display_name, context_window, supported_in_api).
If you want Codex's full `base_instructions` for each model, copy your Codex installation's
bundled `models.json` and append the OpenCode Go entries from the reference catalog.

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
