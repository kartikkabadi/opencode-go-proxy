STATUS: IMPLEMENTED on codex/0.2.0-parity-build (see plans/README.md and DEPLOYMENT.md). Historical planning doc; the shipped code is authoritative.

# Plan 004 - Vision bridge module (full depth)

## Why this matters

Vision is currently a patch inside the streaming path: hardcoded engine,
latest-image-only stub, no cache, blocking sub-call. The destination wants
custom and official models to work perfectly, including local vision
engines. This plan turns vision into a real module. It builds on plan 001
(which lands the urgent latency subset first: hash cache, detail=low /
sips downscale, turn-model engine default, 30s timeout, no retries).

## Current state

- `app.py:733-772` caption_images_in_messages; `app.py:789-813`
  caption_image_via_mimo; engine default mimo-v2.5 (`protocol.py:123`).
- Reference: codex-router `vision-bridge.mjs` (engine auto-pick, 1h hash
  cache, evidence contract, metering, local engines).

## Changes

### 1. Engine adapter interface

- `vision.py` exposes `describe(image, prompt) -> Evidence` where Evidence is
  the structured block (Summary / Text / Layout / Unreadable), with spatial
  guidance preserved for click precision.
- Adapters: remote (chat completions through upstream.py) and local
  (Ollama, llama.cpp server, LM Studio) each behind the same interface.
  One adapter = hypothetical seam; two = real.

### 2. Engine selection

- Auto-pick the cheapest enabled image-capable model from the catalog
  (input_modalities contains image), falling back to mimo-v2.5, honoring
  CODEX_IMAGE_MODEL as an override. Probe local runtimes read-only; missing
  runtimes are simply not enabled.

### 3. Cache + metering (reuse plan 001)

- 1h image-hash cache (bounded), shared structure with plan 001.
- Meter every non-cached caption read with kind=vision.

### 4. Wiring

- app.py caption call sites delegate to vision.describe; the old
  caption_image_via_mimo and CAPTION_PROMPT handling move into vision.py.
- Keep only-latest-image stubbing, but route it through the same module so
  the evidence contract applies everywhere.

## Out of scope

- No config.toml writes. No benchmark UI in this plan (CLI optional, defer
  unless the user asks). No change to non-image turns.

## Verification gates

- `uv run python -m pytest tests -q` green.
- Caption path imports from vision.py; old caption function gone from app.py.
- Local engine probes never crash when a runtime is absent.

## Test plan

- `tests/test_vision.py`: adapter selection order, cache hit/miss/eviction,
  evidence shaping, metered reads, local probes (mock HTTP).
- Integration: MockUpstream returns a caption; second identical image hits
  the cache with no second upstream call.

## Done criteria

Suite green; vision is a module; engines auto-pick; captions metered;
repeated screenshots served from cache.

## Escape hatches

If a local engine adapter is untestable without the runtime, gate its test
on a probe and skip with a clear message. If upstream rejects detail=low,
drop it and rely on the downscale path from plan 001.
