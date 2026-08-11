# Plan 011 - Rate-limit header harvesting into quota state

## Why this matters

Ticket "Config and ops depth" decided rate-limit headers are harvested into
quota state (tray display separate). codex-router parses x-ratelimit-* and
anthropic-ratelimit-* headers into quota cards; the proxy currently reads
none.

## Current state

- upstream.py call_upstream_chat and streaming.py do not surface response
  headers.
- meter.py owns state-dir writes.

## Changes

1. In upstream.py and streaming.py, capture response headers
   (x-ratelimit-remaining, x-ratelimit-limit, x-ratelimit-reset,
   anthropic-ratelimit-* where present) from upstream responses.
2. Add quota.py (small module): parse the headers into a quota snapshot
   {provider, limit, remaining, resetAt, sampledAt}, keep the latest per
   provider, and write/read a state-dir JSON file
   (quota-state.json) with atomic writes.
3. Record a quota snapshot after each upstream 200 response; expose it at
   GET /quota (JSON) for the menu bar state contract later.
4. Tests: header parsing (each naming scheme), snapshot persistence,
   /quota endpoint, no-header case.

## Out of scope

- No menu bar UI changes (separate plan); no config.toml writes.

## Verification gates

- uv run python -m pytest tests -q green; uvx ruff check src tests clean.

## Escape hatches

If the Go upstream does not send rate-limit headers, the module degrades to
an empty snapshot; do not invent numbers.
