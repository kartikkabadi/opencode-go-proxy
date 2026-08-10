# OpenCode Go Proxy

[![CI](https://github.com/kartikkabadi/opencode-go-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/kartikkabadi/opencode-go-proxy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Dependencies: zstandard](https://img.shields.io/badge/dependencies-zstandard-blue.svg)](#)

Use your [OpenCode Go](https://opencode.ai/docs/go) subscription in the [Codex app](https://github.com/openai/codex).

Codex expects a Responses API (`/v1/responses`). OpenCode Go exposes an OpenAI-compatible
Chat Completions API (`/v1/chat/completions`). This proxy bridges that gap in one local process:

```text
Codex app
    │
    │  POST /v1/responses (Responses API)
    ▼
opencode-go-proxy  ←── localhost:8787, one runtime dep (zstandard)
    │
    │  POST /v1/chat/completions (Chat Completions API)
    ▼
OpenCode Go  ────── 13 models: DeepSeek, GLM, Kimi, MiMo, MiniMax, Qwen
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

When images are present in a turn with tools, the proxy captions the latest image
(older ones are stubbed) and routes the main turn to your configured model. The caption
engine auto-picks the cheapest image-capable model from the catalog (`input_modalities`
contains image) with `mimo-v2.5` as the fallback, or a local vision runtime (Ollama,
llama.cpp server, LM Studio) that answers a read-only probe. Captions are cached by
image bytes for an hour, sent with `detail: low` to cut upstream vision cost, and every
non-cached read is metered with `kind=vision`; the caption budget is 30s with no retries,
so a failed caption degrades to a placeholder instead of stalling the turn. Override the
engine with `CODEX_IMAGE_MODEL` or `OPENCODE_GO_PROXY_CAPTION_MODEL` (a model slug,
`local`, or default `auto` = catalog pick); configure a local runtime with
`OPENCODE_GO_PROXY_VISION_LOCAL_BASE_URL` / `OPENCODE_GO_PROXY_VISION_LOCAL_MODEL`.
Disable `detail: low` with `OPENCODE_GO_PROXY_CAPTION_DETAIL=none` (a rejected detail
value also falls back to a `sips`-downscaled image with no detail).

## API key

The proxy resolves your OpenCode Go API key in this order:

1. `$OPENCODE_GO_API_KEY` environment variable
2. macOS keychain entry `opencode-go-api-key` (override with `CODEX_KEYCHAIN_SERVICE`; macOS only)

```bash
# Option 1: env var (works everywhere)
export OPENCODE_GO_API_KEY="your-key-here"

# Option 2: macOS keychain (macOS only)
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
- Cached image captioning when tools are present: cheapest catalog vision engine (or a probed local runtime) by default, `detail: low` input, 30s no-retry budget, MiMo V2.5 fallback, reads metered with `kind=vision`
- SSRF protection on image URLs (`data:image/` and `https://` only)
- Configurable body cap, bind address guard, keychain credential resolution
- Local health and model-list endpoints
- Prefix caching: byte-stable request prefixes plus `include_usage`, with per-model hit ratio on `/cache`
- Honest usage meter: append-only `usage-events.jsonl` in the state dir (truncated or empty responses never count as success)
- Upstream retry with bounded exponential backoff on transient failures (429/5xx/network/timeout)
- Ops CLI: `doctor` (self-check), `smoke-test` (live upstream probe), `support-bundle` (redacted tarball)
- Spawned threads inherit the parent session's model (`create_thread` / `send_message_to_thread`)
- Verbatim `/v1/chat/completions` passthrough (stream and non-stream): the upstream status and body are relayed byte-for-byte, including the upstream's own error body, and `/v1/messages` answers an explicit `400`
- WebSocket upgrade requests answered with `426 Upgrade Required` (desktop app falls back to HTTP streaming)
- zstd-compressed request bodies decompressed (`Content-Encoding: zstd`; the desktop app sends them)
- Single-port guard in the menu bar app: refuses Start when 8787 is already owned

## Prefix caching

OpenCode Go bills cached reads at a fraction of normal input (`Cached Read` in the
pricing table), and its usage estimates assume most tokens per request are served from
cache. For that to happen the request prefix must be byte-stable: the same system
prompt, tools, and earlier conversation, in the same order, on every request.

The proxy is built for exactly that:

- The system prompt (`instructions` / `developer` roles) is always the first message.
- Conversation history is appended in order; translation is deterministic, so the
  serialized prefix is identical across requests in a session — and across sessions
  that share the same system prompt and tools.
- Streaming requests set `stream_options: { "include_usage": true }` so the upstream
  reports its cache accounting (`prompt_cache_hit_tokens` / `cached_tokens`) in the
  stream; without this most OpenAI-compatible endpoints omit usage entirely.
- Cache hits are surfaced to Codex in the standard Responses shape
  (`usage.input_tokens_details.cached_tokens`), so the app shows them in its token
  display.

### Checking the hit ratio

Every request with cache accounting is logged (look for `"cache": {...}` on
`response.converted` trace lines). The aggregate is also exposed on a metrics
endpoint:

```bash
curl http://127.0.0.1:8787/cache
```

```json
{
  "models": [
    {
      "model": "deepseek-v4-flash",
      "cache_hit_tokens": 100000,
      "cache_miss_tokens": 1000,
      "requests": 50,
      "hit_ratio": 0.990099
    }
  ],
  "totals": { "...": "..." }
}
```

Within a session, every request after the first shares the full previous prefix, so
cache-eligible requests typically run at 99%+ hit ratio; only the genuinely new delta
misses. The first request of a brand-new context always misses (nothing is cached yet).

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

### macOS (launchd)

A launchd plist is included at `contrib/launchd/com.opencode-go.proxy.plist`.
Copy it to `~/Library/LaunchAgents/` and load:

```bash
mkdir -p ~/Library/LaunchAgents ~/.codex/logs
cp contrib/launchd/com.opencode-go.proxy.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.opencode-go.proxy.plist
```

The proxy is designed for launchd's `KeepAlive` — it restarts on crash and
starts at login. Logs go to `~/.codex/logs/opencode-go-proxy.{log,err}`.

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

**One HTTP port only.** The proxy binds a single listener: `OPENCODE_GO_PROXY_PORT`
(default `8787`). There is no admin port, control channel, or secondary service. If
something else already listens on the port, the proxy fails to bind — check with
`lsof -nP -iTCP:8787 -sTCP:LISTEN` before starting a second instance (for example, do
not run the menu bar app's proxy and the launchd agent at the same time).

**Short provider name.** The long "opencode go/" label in the Codex model picker comes
from the provider config in `~/.codex/config.toml`, not from this proxy. Shorten it by
editing the provider `name`:

```toml
[model_providers.opencode-go]
name = "Go"  # shows as "Go" instead of "opencode go/"
```

The reference catalog's per-model `display_name` values are already short
("DeepSeek V4 Flash", "Kimi K2.7 Code", etc).

## Model catalog

The proxy serves `/v1/models` from a runtime catalog in the state dir
(`OPENCODE_GO_PROXY_STATE_DIR`, default `~/.codex/opencode-go-proxy/`). At startup it renders
the state-dir compact catalog (or the checked-in seed at `contrib/opencode-go-models.json`)
immediately, then refreshes in the background: models.dev discovery merges in additively,
TTL-gated so a fresh catalog never hits the network, and the full catalog is written to
`opencode-go-catalog.json` under the state dir. Runtime refresh never writes the repo's
`contrib/` files; maintain the checked-in seed with `opencode-go-proxy --refresh-catalog`.

Rendered models follow the exact key set Codex reads in codex-router's `merged-models.json`:
`multi_agent_version` lives at the model top level, `comp_hash`/`availability_nux`/`tool_mode`
are present, and `model_messages` carries `approvals`, `collaboration_modes`, `permissions`,
`token_budget`, and `auto_review` instead of dropping them. A parity test pins the renderer to
that key set.

To keep Codex from printing a model metadata warning every turn, point `model_catalog_json` at
a full-shape catalog. The rendered one in the state dir works; copy it where you like:

```bash
mkdir -p ~/.codex/model-catalogs
cp ~/.codex/opencode-go-proxy/opencode-go-catalog.json ~/.codex/model-catalogs/opencode-go.json
```

```toml
model_catalog_json = "/home/you/.codex/model-catalogs/opencode-go.json"
```

The catalog ships with the `ModelsCache` wrapper (`fetched_at`/`etag`/`client_version`/`models`).
Codex 0.142+ desktop app requires all four top-level fields — a bare `{"models": [...]}` catalog
causes the model picker to fall back to "Custom" instead of showing the full list. The CLI
(`codex debug models`) tolerates the bare format, so this only surfaces in the desktop app.

### Local model overlay

Custom models are layered on top of the merged catalog at runtime, never by editing
`contrib/`. `user-models.json` in the state dir holds entries keyed by slug: a full record adds
a model, a partial record edits display fields, `"hide": true` hides one:

```json
{
  "version": 1,
  "models": [
    {"slug": "my-custom-model", "display_name": "My Custom", "context_window": 200000},
    {"slug": "deepseek-v4-flash", "display_name": "Flash (edited)", "priority": 3},
    {"slug": "deepseek-v4-pro", "hide": true}
  ]
}
```

Hidden-model flags from `model-picker.json` (`{"version": 1, "hidden": ["slug"]}`) hide models
from the picker too. Newly added models get a seven-day `availability_nux` announcement,
tracked in `announced-models.json` under the state dir. The proxy re-reads the rendered catalog
on each `/v1/models` request (cached by file mtime), so editing the overlay and refreshing grows
the live list without a restart.

If you want Codex's full `base_instructions` for each model, copy your Codex installation's
bundled `models.json` and append the OpenCode Go entries from the reference catalog (keep the
`ModelsCache` wrapper).

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

## Troubleshooting

**Model metadata warning every turn**
Set `model_catalog_json` in Codex config and copy the rendered catalog:
`cp ~/.codex/opencode-go-proxy/opencode-go-catalog.json ~/.codex/model-catalogs/opencode-go.json`

**Connection refused on localhost:8787**
Proxy isn't running. Start it: `opencode-go-proxy` or check `launchctl list | grep opencode`.

**Port already in use**
Only one proxy instance can bind 8787. Confirm what is listening:
`lsof -nP -iTCP:8787 -sTCP:LISTEN`. If a stale instance holds the port, stop it before
starting another.

**API key not found**
Set `OPENCODE_GO_API_KEY` env var or add to macOS keychain:
`security add-generic-password -a "$USER" -s opencode-go-api-key -w`

**Upstream rate limited (429)**
OpenCode Go has 5-hour/weekly/monthly usage limits. Switch to a cheaper model (DeepSeek V4 Flash or MiMo V2.5) to stretch your quota. See [usage limits](https://opencode.ai/docs/go#usage-limits).

**Streaming not working**
Codex sends `stream: true` — the proxy handles this. If you see no SSE events, check stderr trace for `upstream.error` or `upstream.network_error`.

**Codex says "model is not supported when using ChatGPT account"**
You used `codex -m deepseek-v4-flash` instead of `codex -p deepseek-v4-flash`. The `-m` flag only changes the model name, not the provider. Use `-p` to select a profile.

## Development

```bash
uv run python -m pytest tests -v
uvx ruff check
uv build
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## License

MIT. See [LICENSE](LICENSE).
