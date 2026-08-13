STATUS: IMPLEMENTED on codex/0.2.0-parity-build (see plans/README.md and DEPLOYMENT.md). Historical planning doc; the shipped code is authoritative.

# Plan 006 - Auth transport guard (zero config)

## Why this matters

Ticket "Auth and secret model for the proxy" decided Option A: transport
guard, zero config. The listener stays loopback-bound; requests with a
missing Host get 400, non-loopback Host get 403, browser-originated requests
(Origin / Referer / Sec-Fetch-Site) get 403, non-JSON content types get 415,
and OPTIONS stays unhandled so browser preflight is blocked.

## Current state

- app.py binds 127.0.0.1 by default (--bind) and has no Host/Origin/Referer
  or content-type checks.
- codex-router reference shapes: 403 {"error":{"type":"browser_request_rejected"}},
  415 for non-JSON, no CORS.

## Changes (in app.py or a small guards.py used by dispatch)

1. Host validation on every request: missing Host -> 400
   {"error":{"type":"invalid_host","message":"missing Host header"}}.
   Host must be normalized first (strip the port and IPv6 brackets; e.g.
   `127.0.0.1:8787` -> `127.0.0.1`) and then checked against
   {127.0.0.1, localhost, ::1}; anything else -> 403
   {"error":{"type":"invalid_host","message":"request host is not allowed"}},
   unless OPENCODE_GO_PROXY_ALLOW_REMOTE=1 (explicit opt-in for deliberate
   non-loopback binds).
2. Browser rejection: if any of Origin / Referer / Sec-Fetch-Site is present
   -> 403 {"error":{"type":"browser_request_rejected","message":
   "Browser-originated requests are not accepted by the local proxy."}}.
3. Content-type check on POST to /v1/* and /v1/responses: Content-Type must
   be application/json (allow application/json; charset=...) -> else 415
   {"error":{"type":"unsupported_media_type","message":"Proxy requests
   require Content-Type: application/json."}}.
4. OPTIONS: leave unhandled (do not add CORS headers) so preflight is
   blocked; the default handler response is fine.

## Out of scope

- No caller-capability path secret, no secret files, no config.toml writes
  (deferred unless non-loopback bind).
- No changes to the keychain/env secret resolution.

## Verification gates

- uv run python -m pytest tests -q green.
- uvx ruff check src tests clean.
- All rejections use the proxy's existing error JSON shape and exact status
  codes above.

## Test plan

- tests/test_auth_guard.py: each rejection (missing Host, evil Host,
  Origin, Referer, Sec-Fetch-Site, text/plain POST, OPTIONS preflight), and
  the allow-remote env escape hatch; valid loopback + application/json
  requests still pass through.

## Done criteria

All gates pass; a browser page cannot POST to the proxy and DNS rebinding
cannot read responses.

## Escape hatches

If a legit local client sends Referer or Origin (e.g. a local tool), the
block is by design; the allowlist env is for Host only. Do not weaken the
browser rejection without recording an ADR.
