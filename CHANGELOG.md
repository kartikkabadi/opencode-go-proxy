# Changelog

## [0.4.0] - 2026-08-14

### Added

- OpenCode Zen support: the proxy now serves both OpenCode Go and OpenCode Zen.
  Zen models are auto-discovered from `https://opencode.ai/zen/v1/models` (no
  auth), merged with models.dev metadata, and appear in the catalog and
  `/v1/models` as `zen/<id>` slugs. Requests route per family — claude/qwen to
  Anthropic Messages (`/zen/v1/messages`, `x-api-key`), gemini to the Gemini API
  (`/zen/v1/models/<id>`, `x-goog-api-key`), gpt/grok to the Responses API
  (`/zen/v1/responses`, relayed verbatim), everything else to
  chat/completions — with each family's stream translated back to one uniform
  Responses stream. Auth uses the same key as Go
  (`OPENCODE_GO_API_KEY` / `opencode-go-api-key` keychain) with the header
  mapped per family. Zen turns meter with `provider="zen"` so they never count
  against the Go quota. Free models (`deepseek-v4-flash-free`, `mimo-v2.5-free`,
  `big-pickle`) need no credits.
- Usage in the menu bar: `GET /state` gains `usage.go` (OpenCode Go usage
  polled from `https://opencode.ai/zen/go/v1/usage` with a 60s TTL, `null` on
  fetch failure), `usage.goLimits` (fixed Go dollar budgets: $60/month,
  $30/week, $12 per rolling 5 hours), and `usage.zen` (today's turns/tokens and
  a seven-entry last-7-day token rollup from the local meter), alongside the
  existing all-provider keys.

## [0.3.1] - 2026-08-14

### Fixed

- Catalog `availability_nux` is emitted in the `{message: string} | null`
  object form the Codex app schema expects instead of a bare string;
  string-form nux arriving from upstream records is normalized, with empty
  strings mapped to `null`.

## [0.3.0] - 2026-08-13

### Added

- Native model coexistence: models from your logged-in Codex account
  (`codex debug models`, filtered of every `opencode-go/` slug) are merged
  with the OpenCode Go catalog and served by `/v1/models`. Native slugs route
  verbatim to `https://chatgpt.com/backend-api/codex`
  (`OPENCODE_GO_PROXY_NATIVE_BASE_URL` overrides); everything else translates
  to OpenCode Go as before. Official OAuth and API setups are untouched.
- App tool capture: `codex_app` tool definitions are snapshotted via
  `codex debug prompt-input` and merged into session-spawn requests
  (`contrib/codex-app-tools.json` fallback); `web_search_preview` translates
  to a chat function instead of being dropped.
- Compaction requests with query strings now route correctly (404 fixed).
- Multi-agent config v2 (`multi_agent_version: "v2"`) with agent sync, an
  `install-skills` ops command, and a `refresh-runtime` command (native
  recapture + merged re-render) that the menu bar's Refresh Catalog calls.
- macOS launchd agent removed (`contrib/launchd/`); the menu bar app owns the
  proxy process on macOS.

### Changed

- `MODEL_ALIASES` is gone: unknown non-native slugs fall back to the default
  model, same as before.
- Meter events carry a `provider` tag (`native` vs `opencode-go`).


- Ops contracts match the codex-router reference (plan 010): `smoke-test` posts
  a marker prompt through the local proxy at `http://127.0.0.1:8787/v1/responses`
  and asserts the marker comes back (custom base via `--base-url` or
  `OPENCODE_GO_PROXY_BASE_URL`); `support-bundle` emits a single JSON document
  (schemaVersion 1, mode 0600) with version, generatedAt, env summary, redacted
  config snapshot, meter tail, log tail, catalog model count, and doctor checks
  instead of a tar.gz; `doctor` gains reference-style `ok`/`warn`/`fail` checks
  (config file presence, catalog readability, port free/owned, log writability,
  best-effort upstream reachability) plus `--fix`, which repairs only what is
  safe without writing config.toml.
- Session-model inheritance matches the reference (plan 009): only
  `create_thread` calls get the session model injected and
  `chatgptWorkCloud` targets are skipped; `send_message_to_thread` is no
  longer rewritten.
- Catalog discovery sends an identifying `User-Agent`
  (`opencode-go-proxy/<version>`) because models.dev answers 403 to
  urllib's default UA (plan 009).
- Image routing (plan 009): a non-tools image turn keeps the requested
  model when the catalog marks it image-capable (`input_modalities`
  contains `image`), otherwise it falls back to `CODEX_IMAGE_MODEL` /
  `mimo-v2.5`; the image-plus-tools caption path is unchanged.
- Image fallback (plan 009b): when the upstream rejects a non-tools image
  turn at runtime with a caption-fallback 4xx (400/404/415/422), the proxy
  captions the images through the vision module and retries the same
  requested model once instead of failing the turn. The split-turn
  (image plus tools) caption path and the verbatim `/chat/completions`
  passthrough are unchanged.
- Usage meter events now match the codex-router reference schema (plan 008):
  `at` is ISO 8601 UTC, token and duration fields are camelCase
  (`inputTokens` / `outputTokens` / `totalTokens` / `durationMs`), and every
  event carries `meteringVersion: "opencode-go-proxy/1"` plus
  `provider: "opencode-go"`. The legacy snake_case/epoch spelling was dropped
  rather than double-written because no live consumer reads the file today
  (the menu bar is reworked in a later plan); if a consumer needs the old
  fields, they return additively.

- Rate-limit harvesting (plan 011): OpenAI-style (`x-ratelimit-*`) and
  Anthropic-style (`anthropic-ratelimit-*`) response headers from upstream 200s
  are parsed into per-provider quota snapshots (`limit` / `remaining` / `resetAt`
  / `sampledAt`), the latest snapshot per provider is kept, and persisted
  atomically to `quota-state.json` in the state dir. `GET /quota` (and
  `/v1/quota`) returns the state; a headerless upstream degrades to an empty
  snapshot.
- Config manager (plan 012): `config enable|disable|status [--json]` owns a
  marker-commented block in `~/.codex/config.toml` (`# BEGIN
  opencode-go-proxy-managed` to `# END opencode-go-proxy-managed`). Enable
  writes `openai_base_url` plus `model_catalog_json` (state-dir catalog) and
  refuses to replace user-owned values; disable removes only the block and
  deletes the file when the block was its only content. Codex Voice realtime
  keys (`experimental_realtime_webrtc_call_base_url` and
  `experimental_realtime_ws_base_url`) are added only when the user has not
  set them, so Voice stays on native endpoints. Tests and CI run against a
  temp file via `OPENCODE_GO_PROXY_CONFIG_PATH`; the real config.toml is a
  gated deploy step.

### Added

- Base-URL overrides (plan 010): the upstream chat base resolves as the
  `--chat-base-url` flag, then `OPENCODE_GO_BASE_URL`, then
  `OPENCODE_ZEN_BASE_URL`, then the legacy `CHAT_COMPLETIONS_BASE_URL`, then the
  built-in default.
- Ops install/status (plan 010): `install` and its `setup` alias copy the
  launchd plist into `~/Library/LaunchAgents` and load the agent on macOS,
  gated on an explicit `--yes` flag (running it is a deploy step); `status`
  reports running state, port ownership, launchd state, and log paths.
- Doctor/keychain seam (plan 010): `secrets.api_key_source()` reports where the
  key would resolve (env or keychain service) without reading the value, so
  doctor never prints the credential.
- Correctness contract (plan 007): an upstream 200 that streams no
  text, tool call, or reasoning is retried once with the identical request
  (terminal events held, ids reused); a second empty stream answers
  `response.error` code `empty_completion`. Upstream `input_tokens: 0` is
  estimated as `max(1000, ceil(prompt_bytes / 3.3))` capped at the model's
  context window and surfaced as `estimatedInputTokens` (kill switch
  `OPENCODE_GO_PROXY_ESTIMATE_ZERO_INPUT=0`; self-disables once the upstream
  reports real tokens again). The keepalive comment thread now runs until the
  stream truly ends, with writes serialized so comments never interleave into
  data frames.
- Auth transport guard (plan 006): zero-config protection for the loopback
  listener - a missing Host header answers `400 invalid_host`, a non-loopback
  Host answers `403 invalid_host` unless `OPENCODE_GO_PROXY_ALLOW_REMOTE=1`,
  requests carrying Origin / Referer / Sec-Fetch-Site answer
  `403 browser_request_rejected`, non-JSON POSTs to the API paths answer
  `415 unsupported_media_type`, and OPTIONS stays unhandled so browser
  preflight is blocked.
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
- Menu bar state contract (plan 013): `GET /state` (and `/v1/state`) returns
  one JSON document - `status`, `port`, `upstream`, the latest quota snapshot
  by `sampledAt` (`{provider, remaining, limit?, resetAt?}` or `null`),
  `usage` with `todayTurns` / `todayTokens` / a stable seven-entry `last7d`
  token list (local calendar days, oldest first), and `model` (most recent
  meter event, else the default). A missing or corrupt meter/quota file
  degrades to zeros or `null`. The Swift menu bar renders the Standard tier
  from that contract: quota card with reset countdown, today's turns/tokens,
  a 7-day usage bar list, and model/upstream rows, keeping the single-port
  guard and existing controls.

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
- Caption engine: `OPENCODE_GO_PROXY_CAPTION_MODEL=auto` picks the cheapest
  image-capable catalog model (an enabled local runtime first);
  `CODEX_IMAGE_MODEL` overrides, and `mimo-v2.5` is the fallback when the picked
  engine rejects image input (4xx).
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

[Unreleased]: https://github.com/kartikkabadi/opencode-go-proxy/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.4.0
[0.3.1]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.3.1
[0.3.0]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.3.0
[0.1.2]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.1.2
[0.1.1]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.1.1
[0.1.0]: https://github.com/kartikkabadi/opencode-go-proxy/releases/tag/v0.1.0
