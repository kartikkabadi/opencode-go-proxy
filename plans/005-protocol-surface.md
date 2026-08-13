STATUS: IMPLEMENTED on codex/0.2.0-parity-build (see plans/README.md and DEPLOYMENT.md). Historical planning doc; the shipped code is authoritative.

# Plan 005 - Protocol surface: chat/completions passthrough + /messages + WS 426 test

## Why this matters

Ticket "Protocol surface and Voice handling" decided: ship /v1/chat/completions
(verbatim upstream relay, SSE byte-for-byte with keepalive), answer /messages
with an explicit 400 (single OpenAI-compatible provider), keep the WS 426
rejection, and verify the 426 copy in place with a test.

## Current state (codex/0.2.0-parity-build)

- app.py is thin dispatch: do_POST routes /v1/responses, /v1/models, /health.
- streaming.py owns the SSE engine for /v1/responses.
- upstream.py owns call_upstream_chat (non-stream) with the retry policy.
- WS 426 rejection exists in app.py (_reject_websocket_upgrade) with NO
  exercising test (verify-in-place decision).

## Changes

### 1. /chat/completions + /v1/chat/completions passthrough

- In app.py do_POST, route chat/completions to a passthrough handler:
  - stream=false: call_upstream_chat, relay upstream status and JSON body
    verbatim (with upstream's own error body, not proxy_error).
  - stream=true: relay upstream SSE byte-for-byte. Wrap with the same 15s
    keepalive comment mechanism the responses stream uses, stop on client
    disconnect, and relay upstream status/body on non-200.
- Missing key: 401 like the responses path. Body size limit: reuse the
  existing max-body guard.

### 2. /messages + /v1/messages explicit 400

- Return 400 JSON error: {"error": {"type": "invalid_request_error",
  "message": "This proxy serves a single OpenAI-compatible provider via
  /v1/chat/completions and /v1/responses; /messages is not supported."}}

### 3. WS 426 verify-in-place test

- Add an integration test exercising the WebSocket upgrade rejection: a
  request with Upgrade: websocket + Connection: Upgrade + the realtime
  headers returns 426 with the exact rejection body. Do not change the
  response bytes (they match the reference).

## Out of scope

- No config.toml writes. No Voice endpoint wiring (managed config is a
  separate plan). No changes to /v1/responses behavior.

## Verification gates

- uv run python -m pytest tests -q green (current baseline: 454 tests).
- uvx ruff check src tests clean.
- The passthrough is byte-identical: assert relayed body equals the mocked
  upstream body.

## Test plan

- tests/test_protocol_surface.py: JSON passthrough, SSE verbatim relay with
  keepalive, upstream 429/500 relay, missing key 401, /messages 400, WS 426.

## Done criteria

All gates pass; /v1/chat/completions works for stream and non-stream;
/messages answers 400; WS 426 has an exercising test.

## Escape hatches

If the upstream rejects a passthrough body field (e.g. stream_options), relay
the request exactly as received from the client and report the 4xx upstream
body verbatim. If SSE relay with keepalive interleaves comments into data
frames, serialize writes with a lock like the responses stream.
