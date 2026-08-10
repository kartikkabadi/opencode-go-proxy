# Changelog

## [Unreleased]

### Added

- Protocol surface (plan 005): `/v1/chat/completions` (and `/chat/completions`)
  is a verbatim passthrough - non-stream relays the upstream status and JSON
  body byte-for-byte (including the upstream's own error body), stream relays
  the upstream SSE unchanged with the same 15s keepalive comment mechanism the
  responses stream uses and stops on client disconnect; `/v1/messages` (and
  `/messages`) answer an explicit `400 invalid_request_error`; the WebSocket
  426 rejection now has a test asserting the exact response body.
- Vision bridge module (plan 004): `vision.describe()` returns structured
  `Evidence` (summary / text / layout / unreadable); the caption engine auto-picks the
  cheapest image-capable catalog model or a probed local runtime (Ollama, llama.cpp
  server, LM Studio); non-cached caption reads are metered with `kind=vision`; the old
  caption path now lives entirely in `vision.py`.
- Catalog contract with overlay and runtime reload (plan 003): `render_full_catalog`
  projects each model through the canonical key set Codex reads in
  `merged-models.json` (`multi_agent_version` at the model top level, plus
  `comp_hash`, `availability_nux`, and `approvals`/`collaboration_modes`/
  `permissions`/`token_budget`/`auto_review` in `model_messages`), backed by a
  key-set parity test against a sampled fixture.
- Runtime catalog lives under the state dir (`OPENCODE_GO_PROXY_STATE_DIR`,
  default `~/.codex/opencode-go-proxy/`); refresh never writes the repo's
  `contrib/` files, and startup renders the state-dir compact (or the seed)
  instantly while discovery runs in the background.
- `known_models()` replaces the frozen `KNOWN_MODELS`: the live slug set is
  cached by catalog file mtime, so `/v1/models` and model routing pick up a
  refreshed catalog without a restart (`reload_known_models()` forces it).
- User overlay for custom models: `user-models.json` (add / hide / edit display)
  plus hidden-model flags from `model-picker.json`, seven-day `availability_nux`
  announcements tracked in `announced-models.json`.

## [0.2.0] - 2026-08-10

### Added

- Prefix-cache support for OpenCode Go models: streaming requests now set
  `stream_options.include_usage`, the proxy parses the upstream's cache accounting
  (`prompt_cache_hit_tokens` / `prompt_tokens_details.cached_tokens`), surfaces it to
  Codex in `usage.input_tokens_details.cached_tokens`, logs it per request, and
  exposes aggregate hit/miss/ratio on a `/cache` (and `/metrics`) endpoint.
- `CacheTracker` in `src/cache.py`: thread-safe per-model hit/miss/ratio accounting.
- Key resolution falls back to `OPENCODE_API_KEY` and the `codex-router-opencode-go`
  keychain service, in addition to `OPENCODE_GO_API_KEY` / `opencode-go-api-key`.
- Spawned threads inherit the session model: `create_thread` and
  `send_message_to_thread` function calls get the parent thread's model injected
  when the caller omits one.
- Honest usage meter in `src/meter.py`: append-only `usage-events.jsonl` in the state
  dir with per-turn status, duration, tokens and retries. Truncated streams record
  `streamAborted` and never count as success; empty completions record
  `emptyCompletion`. Metering failures never break a live request.
- Upstream retry with bounded exponential backoff on transient failures
  (429/5xx/network/timeout), configured via `OPENCODE_GO_PROXY_MAX_RETRIES`
  (default 2) and `OPENCODE_GO_PROXY_RETRY_BASE_MS`.
- Ops CLI commands: `opencode-go-proxy doctor` (self-check), `smoke-test` (real
  upstream probe) and `support-bundle` (namespaced tarball of logs, config and
  meter, with secret values redacted).
- Agent skill `skills/opencode-go-proxy/SKILL.md`: orientation for operating the
  proxy (start/stop, ops commands, prefix caching, config, reliability).
- Caption latency: identical screenshot bytes are captioned once per hour (in-process
  byte-keyed cache, 256-entry bound), so repeat tool calls on the same screen skip the
  upstream round trip.
- Caption engine defaults to the turn model (`OPENCODE_GO_PROXY_CAPTION_MODEL=auto`);
  `CODEX_IMAGE_MODEL` still overrides, and `mimo-v2.5` remains the fallback when the
  turn model rejects image input (4xx).
- Caption images are sent with `detail: low` (`OPENCODE_GO_PROXY_CAPTION_DETAIL=none`
  disables); if the upstream rejects the detail value, the proxy retries once with a
  `sips`-downscaled JPEG (or the original URL) and no detail.
- Tests: cache parsing (both DeepSeek and OpenAI usage shapes), tracker accounting,
  `/cache` endpoint, streaming `stream_options`, cache passthrough to Codex,
  meter recording, upstream retry behavior, and the ops commands.

### Changed

- Native macOS menu bar app in `macos/MenuBarApp` (Swift/AppKit, SwiftPM): short status
  icon, live health check, start/stop of the proxy as a child process, open logs,
  reveal log file, copy port. It refuses to Start when port 8787 is already owned
  (single-port guard). Build with `swift build` in `macos/MenuBarApp` (macOS 13+).
- README: document that the proxy exposes a single HTTP port (`OPENCODE_GO_PROXY_PORT`,
  default 8787) with no admin/control channel, how to verify what is listening
  (`lsof -nP -iTCP:8787 -sTCP:LISTEN`), and how to shorten the Codex provider label
  (edit `[model_providers.opencode-go] name` in `~/.codex/config.toml`).

### Fixed

- zstd request bodies: the Codex desktop app sends `/v1/responses` bodies
  zstd-compressed whenever it is authenticated; the proxy now decompresses
  `Content-Encoding: zstd` (and gzip) so the desktop app works. Unsupported
  encodings return an explicit 400 instead of crashing on a magic byte.
- Reference catalog `contrib/opencode-go-catalog.json` now ships with the `ModelsCache` wrapper
  (`fetched_at`/`etag`/`client_version`/`models`). Codex 0.142+ desktop app requires all four
  top-level fields — the previous bare `{"models": [...]}` caused the model picker to fall back
  to "Custom" instead of showing the full list. The CLI tolerated the bare format, so this only
  surfaced in the desktop app.
- SIGTERM graceful shutdown: `serve_forever` now runs on a background thread so the signal
  handler's `server.shutdown()` no longer deadlocks on the main thread, leaving the process
  unkillable via SIGTERM (launchd/systemd stop, menu bar Stop).
- WebSocket upgrade requests (Codex desktop app realtime) are answered with an explicit
  `HTTP/1.1 426 Upgrade Required` instead of the previous HTTP/1.0 404, which surfaced as
  "WebSocket protocol error: HTTP version must be 1.1 or higher" before the app fell back
  to HTTP streaming.
- Image caption budget: default timeout 60s -> 30s, and caption sub-calls no longer
  retry transient failures, so a bad caption leg costs at most ~30s instead of stacking
  retries.

## [0.1.2] - 2026-06-21

Bug fixes + removed AUR packaging.

### Fixed

- `call_upstream_chat` now catches `json.JSONDecodeError` — invalid JSON from upstream returns 502 instead of crashing
- Streaming crash: if `handle_streaming_request` raised after SSE headers sent, sends `response.error` SSE event instead of corrupted HTTP response
- Streaming + missing API key: `resolve_api_key` moved before `response.created` so error event reaches client
- README launchd path: `~/Library/.codex/logs` → `~/.codex/logs` (matches plist)
- LICENSE copyright year 2025 → 2026

### Removed

- AUR package (`aur/opencode-go-proxy-git/`) — not launching on AUR
- PyPI — not launching on PyPI; `uvx --from git+...` is the install path

## [0.1.1] - 2026-06-21

Graceful shutdown + launchd service file.

### Added

- SIGTERM handler for graceful shutdown on `launchctl bootout` / `systemctl stop`
- launchd plist at `contrib/launchd/com.opencode-go.proxy.plist`
- README: launchd setup instructions with copy + bootstrap commands

## [0.1.0] - 2026-06-21

Initial public release.

### Added

- Responses `input` to chat `messages` translation
- `instructions` and `developer` roles mapped to system messages
- Function tool schema passthrough
- Custom/freeform tool adaptation (Codex `apply_patch` works)
- `reasoning_content` replay across tool-call turns
- Real-time SSE streaming
- Image captioning via MiMo V2.5 when tools are present
- SSRF protection on image URLs (`data:image/` and `https://` only)
- Configurable body cap, bind address guard
- macOS keychain credential resolution
- Local health and model-list endpoints
- Reference model catalog with all 13 OpenCode Go models
- systemd user service at `contrib/systemd/`
- 41 tests (unit + integration) covering protocol, credentials, HTTP round-trip, alias map, tool calls, streaming tool calls, streaming error handling, streaming crash recovery, invalid upstream JSON, SSRF, and image captioning

### Security

- SSRF validation on image URLs
- Non-negative Content-Length validation
- Generic error messages to client (full bodies only in trace logs)
- No path reflection in 404 responses
- Bind address guard warns on non-localhost
