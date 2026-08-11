# Plan 008 - Usage meter schema: rework to match the reference, additively

## Why this matters

Ticket "Untested copies: verify or rework per feature" decided the usage
meter is reworked to match the codex-router schema: camelCase fields, ISO
8601 timestamps, meteringVersion and provider fields, with the proxy's
extra fields (streamAborted, emptyCompletion, retries, kind) kept additive.
Consumers (menu bar quota cards, codex-router tray) can then read the same
file.

## Current state

- meter.py record_usage_event writes snake_case/epoch-float events to
  usage-events.jsonl: {"at": <epoch float>, "input_tokens": ..., ...}.
- plan 007 added estimatedInputTokens and kind="vision".

## Changes (meter.py)

1. New schema: every event gets camelCase canonical fields:
   {"at": ISO 8601 (e.g. 2026-08-11T04:00:00Z), "inputTokens", "outputTokens",
   "totalTokens", "model", "status", "durationMs", "retries",
   "emptyCompletion", "streamAborted", "kind", "meteringVersion":
   "opencode-go-proxy/1", "provider": "opencode-go"}.
2. Backward compat: keep the legacy snake_case/epoch fields in the same
   event for one release (additive), OR add a migration note. Decide in the
   plan: write BOTH spellings so the live proxy's existing consumers keep
   working, and record the decision in the changelog. Do not double-write
   unless consumers need it; the proxy's own menu bar is the only known
   consumer and it is reworked in a later plan.
3. Update all call sites (streaming.py, vision.py, upstream.py paths) and
   tests that assert the old shape.

## Out of scope

- No codex-router file writes; the proxy writes only its own state dir.

## Verification gates

- uv run python -m pytest tests -q green; uvx ruff check src tests clean.
- A fixture event parses as JSON with the camelCase keys present.

## Escape hatches

If some consumer (menu bar) breaks, add the legacy fields back additively
and note it; do not silently drop either spelling.
