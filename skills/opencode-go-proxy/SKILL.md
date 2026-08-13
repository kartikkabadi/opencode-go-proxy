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

- The proxy is controlled by a native macOS menu bar app (branch icon). It shows live status, Start/Stop, logs, and copy-port, and it stops the child proxy on Quit. It refuses to Start if port 8787 is already owned (single-port guard).
- CLI: `uvx --from git+https://github.com/kartikkabadi/opencode-go-proxy opencode-go-proxy --port 8787`.
- One proxy per port. Do not run the menu bar app and a launchd agent at the same time; stop the launchd agent first (`launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.opencode.go.proxy.plist`).

## Operational commands

- `opencode-go-proxy doctor` - read-only self-check (API key present, /health 200, meter writable, logs present, Codex config points at the proxy). `--json` for machine output; non-zero exit on any failure.
- `opencode-go-proxy smoke-test` - one tiny real chat-completion to upstream; non-zero on failure.
- `opencode-go-proxy support-bundle [--output FILE]` - tarball of version, logs, meter, and redacted config (secret values masked, never bundled raw).
- `opencode-go-proxy --refresh-catalog` - refresh the model catalog from models.dev.

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

- `~/.codex/config.toml`: the provider block points `base_url` at `http://127.0.0.1:8787/v1`; the `[model_providers.opencode-go] name` knob controls the label in the Codex model picker.
- Bind is guarded: the proxy emits a trace-log warning when bound outside localhost (e.g. `--bind 0.0.0.0`). It does not refuse to start.
