# Plan 002 - Split app.py into deep modules

## Why this matters

app.py is 1,021 lines and owns HTTP dispatch, SSE streaming, captioning,
secret resolution, retry policy, and cache accounting. Every new capability
(the protocol surface, Voice, curation, the menu bar state) has to patch the
same file. The destination requires custom and official models to work with
no special cases; that needs seams to attach to, not a growing blob.

## Current state (live/0.2.0-fixed worktree)

- `src/opencode_go_proxy/app.py:816-882` `call_upstream_chat`: non-stream
  upstream call with retry loop.
- `src/opencode_go_proxy/app.py:85-103` retry constants and caption timeout.
- `src/opencode_go_proxy/app.py:889-947` `resolve_api_key` + keychain
  services + cache; duplicated in `src/opencode_go_proxy/ops.py:67-70`.
- The streaming loop (SSE framing, keepalive thread, output_index
  assignment, empty-completion detection) lives inside app.py handlers.
- Convention: stdlib only (single runtime dep zstandard), modules are
  `src/opencode_go_proxy/*.py`, tests in `tests/` with the MockUpstream
  pattern (`tests/test_integration.py`).

## Changes

### 1. `secrets.py` - one secret store seam

- Move `resolve_api_key`, `_api_key_cache`/lock, and the keychain service
  list from app.py into `secrets.py`, keeping the exact resolution order:
  `OPENCODE_GO_API_KEY`, then `OPENCODE_API_KEY`, then macOS keychain
  services (`opencode-go-api-key`, `codex-router-opencode-go`, plus the
  `CODEX_KEYCHAIN_SERVICE` override).
- Update ops.py to import from `secrets.py` and delete its duplicate
  keychain code.
- Callers: app.py handlers, ops.py, future CLIs.

### 2. `upstream.py` - one upstream client with one retry policy

- Move `call_upstream_chat`, retry constants, `_retriable_http_status`,
  `_retry_sleep`, and `ProxyError` into `upstream.py`.
- Add a `max_retries` parameter (defaults to the module constant); the
  caption call passes 0 (plan 001).

### 3. `streaming.py` - the deep streaming engine

- Move the SSE streaming loop out of app.py: framing, the 15s keepalive
  thread (start before captioning, run until the stream truly ends),
  monotonic output_index assignment, empty-completion detection, and usage
  recording.
- Expose a small interface: `stream_chat(handler, payload, config, ...)` so
  `/responses` and `/chat/completions` share it.

### 4. `app.py` - thin dispatch

- app.py keeps route dispatch and request plumbing and imports the modules.
- No behavior change in this plan.

## Out of scope

- No new dependencies. No config.toml writes. No behavior changes to
  protocol translation, catalog, or ops CLI surfaces beyond the import swap.
- Do not combine with plan 004's behavior changes; land this plan first.

## Verification gates

- `uv run python -m pytest tests -q` green after each step.
- `rg "def resolve_api_key" src/opencode_go_proxy` matches only `secrets.py`.
- `rg "def call_upstream_chat" src/opencode_go_proxy` matches only `upstream.py`.
- app.py drops below 400 lines.

## Test plan

- `tests/test_secrets.py`: resolution order (env wins, keychain fallback),
  cache behavior, keychain service list honors the env override.
- `tests/test_upstream.py`: retry counts (429/5xx/network/timeout), the new
  max_retries=0 path, timeout propagation.
- `tests/test_streaming.py`: keepalive starts before a slow first byte and
  stops on every exit path; output_index monotonic; empty-completion flag.

## Done criteria

All verification gates pass; existing suite green; behavior unchanged.

## Escape hatches

If a step breaks an existing integration test that is testing app.py
internals directly, update the test to import from the new module and keep
the assertion identical. If behavior drift appears, STOP and report.
