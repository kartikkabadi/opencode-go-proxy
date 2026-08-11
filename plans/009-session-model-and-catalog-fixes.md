STATUS: IMPLEMENTED on codex/0.2.0-parity-build (see plans/README.md and DEPLOYMENT.md). Historical planning doc; the shipped code is authoritative.

# Plan 009 - Session-model injection align + catalog discovery UA + image routing

## Why this matters

Three small parity fixes from the locked decisions:

1. Session-model injection must match the reference: rewrite spawned-thread
   model for create_thread only (not send_message_to_thread), and skip
   chatgptWorkCloud targets (ticket "Untested copies", rework-to-match).
2. Catalog discovery: models.dev returns 403 for the default urllib UA
   (verified live: a browser UA gets 200). Set a UA header on the discovery
   fetch so additive refresh works.
3. Image routing: non-tools image turns currently route straight to
   mimo-v2.5 regardless of the requested model (protocol.py image path).
   With the catalog now marking every Go model image-capable, route the
   image turn to the REQUESTED model when it is image-capable; fall back to
   the caption path (vision module) when it is not. This kills the last
   hardcoded-model special case.

## Current state

- protocol.py inject_session_model rewrites send_message_to_thread too and
  lacks the chatgptWorkCloud skip.
- catalog.py discover_models fetches models.dev/api.json with urllib default
  UA -> 403.
- protocol.py image routing picks IMAGE_MODEL_DEFAULT for image turns without
  tools.

## Changes

1. inject_session_model: only rewrite create_thread (SESSION_SPAWN_TOOLS =
   {create_thread}); skip targets of type chatgptWorkCloud. Update tests.
2. discover_models: send User-Agent (e.g. "opencode-go-proxy/<version>")
   on the fetch; log and skip gracefully on non-200 (already handled).
3. Image routing: when a non-tools turn has images and the requested model is
   image-capable per known_models() (input_modalities contains image), keep
   the requested model; otherwise caption via the vision module (existing
   caption_images_in_messages path) or fall back to IMAGE_MODEL_DEFAULT.

## Out of scope

- No config.toml writes; no multi-provider routing.

## Verification gates

- uv run python -m pytest tests -q green; uvx ruff check src tests clean.
- Live-checkable: discovery fetch with UA returns 200 (integration test
  asserts the UA header is sent; do not hit models.dev in unit tests).

## Escape hatches

If the requested model rejects image input at runtime (4xx), the caption
path already falls back per plan 001/004 behavior; keep that chain.
