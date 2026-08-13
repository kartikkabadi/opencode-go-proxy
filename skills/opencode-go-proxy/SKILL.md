---
name: opencode-go-proxy
description: Orientation for using the opencode-go-proxy bridge, which lets the Codex app use the OpenCode Go subscription (13 open coding models) through a local Python service with one runtime dependency (zstandard). Explains how traffic flows, how to start and stop the proxy (menu bar app or CLI, single port 8787), the operational commands (doctor, smoke-test, support-bundle), how prefix caching works and how to check the hit ratio, and the config and API-key resolution. Use when working on or operating opencode-go-proxy, or when the Codex app is pointed at 127.0.0.1:8787.
---

# OpenCode Go Proxy

The proxy bridges Codex's Responses API to OpenCode Go's Chat Completions API in one local process on port 8787. It has one runtime dependency (zstandard); the rest is stdlib. It is single-provider and single-port.

```text
Codex app -> POST /v1/responses -> opencode-go-proxy (127.0.0.1:8787) -> POST /v1/chat/completions -> OpenCode Go
```

## Start and stop

- The proxy is controlled by a native macOS menu bar app (branch icon). It shows live status, Start/Stop, logs, and copy-port, and it stops the child proxy on Quit. It refuses to Start if port 8787 is already owned (single-port guard). No launchd agent on macOS; `opencode-go-proxy install` points at the menu bar app.
- CLI: `uvx --from git+https://github.com/kartikkabadi/opencode-go-proxy opencode-go-proxy --port 8787`.
- One proxy per port. Stop any older launchd agent before starting the menu bar app (`launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.opencode-go.proxy.plist`).

## Operational commands

- `opencode-go-proxy doctor` - read-only self-check (API key present, /health 200, meter writable, logs present, Codex config points at the proxy). `--json` for machine output; non-zero exit on any failure.
- `opencode-go-proxy smoke-test` - one tiny real chat-completion to upstream; non-zero on failure.
- `opencode-go-proxy support-bundle [--output FILE]` - tarball of version, logs, meter, and redacted config (secret values masked, never bundled raw).
- `opencode-go-proxy agents-sync` - writes one Codex agent TOML per opencode-go model into ~/.codex/agents (`router_opencode_go_<slug>`) so subagents use the same models; removes agent files this proxy wrote whose model left the catalog.
- `opencode-go-proxy install-skills [--dry-run]` - copies this skill into ~/.codex/skills/opencode-go-proxy/.
- `opencode-go-proxy --refresh-catalog` - refresh the model catalog from models.dev.

## Models: native and opencode-go

The merged model catalog lists both kinds. Models like `gpt-*` and `o*` are native: the proxy passes those requests straight through to the ChatGPT backend (your Codex login), never aliasing them to an OpenCode Go model. `opencode-go/*` models route through the proxy to OpenCode Go. An unknown slug falls back to the default opencode-go model.

## Threads and automations

The app's codex_app tools (threads, automations, spawn_agent, handoff, fork) are available in chat sessions: the proxy snapshots the app's tool list and merges the missing thread/automation tools into requests that did not carry them. No extra config.

## Prefix caching
OpenCode Go bills cached reads cheaply, so request prefixes are kept byte-stable: the system prompt is always first, history is appended in order, and streaming sets `stream_options.include_usage` so the upstream reports its cache accounting. First request of a new context always misses (cold); later requests in a session typically hit 99%+.

Check the ratio: `curl http://127.0.0.1:8787/cache` (per-model and totals). Per-request: grep for `"cache"` in the stderr log.

## API key

Resolved in order: `OPENCODE_GO_API_KEY`, `OPENCODE_API_KEY`, then macOS keychain services `opencode-go-api-key` and `codex-router-opencode-go`. Never print or commit the key.

## Reliability

- Truncated streams are never counted as success: the meter (`usage-events.jsonl`) records `streamAborted` with status 502.
- Transient upstream failures (429/5xx/network/timeout) are retried with bounded backoff (`OPENCODE_GO_PROXY_MAX_RETRIES`, default 2) before surfacing an error.
- The WebSocket upgrade the Codex desktop app attempts for realtime is answered with `HTTP/1.1 426`; the app falls back to HTTP streaming. That is by design, not an error.

## Config

- `opencode-go-proxy config enable` writes one managed block into `~/.codex/config.toml`: `openai_base_url` (the proxy), `model_catalog_json` (the merged native + opencode-go catalog in the state dir), the `multi_agent_v2` feature (only when the installed codex binary accepts it), and an inert `[model_providers.opencode-go]` block (`base_url` 8787, `wire_api` "responses"). User-owned keys are never replaced; `config disable` removes the block.
- Bind is guarded: the proxy emits a trace-log warning when bound outside localhost (e.g. `--bind 0.0.0.0`). It does not refuse to start.
