# Plan 001 - Cut vision caption latency (~16-60s down to seconds)

## Why this matters

Every Codex turn that contains a screenshot blocks on a synchronous remote
caption call before the main model responds. The code comment says a healthy
caption "routinely takes ~16s" and the timeout is 60s with up to 2 retries,
so a bad caption leg costs the user 16-180 seconds of dead time per
screenshot turn. The user reports ~60s captions today. Screenshot turns are
the common case in agent loops, so this is the highest-leverage latency fix
in the proxy.

## Current state (live/0.2.0-fixed worktree)

- `src/opencode_go_proxy/app.py:733-772` `caption_images_in_messages`:
  collects image_url parts, captions ONLY the latest image, stubs older ones
  with "[prior screenshot omitted]".
- `src/opencode_go_proxy/app.py:789-813` `caption_image_via_mimo`: builds a
  caption payload with `stream: False`, `max_tokens: 200`, engine =
  `CODEX_IMAGE_MODEL` or `mimo-v2.5` (`protocol.py:123`), and calls
  `call_upstream_chat` with `timeout_sec` = 60 default
  (`app.py:93-103`).
- `src/opencode_go_proxy/app.py:816-882` `call_upstream_chat`: retries
  transient failures up to `DEFAULT_MAX_RETRIES = 2` times, each attempt
  bounded by the timeout. A slow caption can therefore take ~3x the timeout
  before degrading.
- Keepalive (`app.py:391-409`) already sends SSE comments during the caption
  so the client is not on a silent stream, but the turn still completes late.
- The proxy is stdlib-only by design (single runtime dep: zstandard). No
  Pillow. macOS system `sips` is available for image resizing.

## Root causes (ordered by leverage)

1. No caption cache: the same screen is re-captioned on every tool call.
2. Full-resolution image sent upstream (Codex screenshots are large retina
   images), which dominates vision latency and cost.
3. Engine hardcoded to mimo-v2.5 even though the merged catalog
   (`~/.codex/codex-router/merged-models.json`) marks every OpenCode Go model
   `input_modalities: ["text","image"]` - the fast model used for the turn is
   image-capable too.
4. 60s timeout with 2 retries multiplies worst-case latency.

## Changes (in order; each independently shippable)

### 1. Caption cache keyed by image bytes

- In `caption_image_via_mimo` (or a small new `vision.py` helper), hash the
  raw image bytes (data URL payload or fetched URL bytes) with sha256.
- Keep an in-process dict `{hash: (caption, expiry)}`, TTL 1h, bounded to
  ~256 entries (evict oldest). Guard with a lock.
- On hit: return the caption with no upstream call. On miss: caption, store,
  return.
- Verification: integration test sends two turns with the SAME image bytes;
  assert the second makes no upstream call (MockUpstream call count == 1).

### 2. Cheap image input: `detail: "low"` first, `sips` downscale fallback

- On the caption image part, set `image_url.detail = "low"` (OpenAI-compatible
  convention) if the upstream accepts it. Zero dependencies.
- If upstream errors on unknown detail values (verify with a live scratch
  run), fall back to downscaling data-URL images with macOS `sips`:
  decode base64 to a temp file, `sips -Z 1600` (cap longest edge), re-encode
  JPEG q85, re-embed. Only for the caption sub-call; never touch the main
  payload.
- Verification: live scratch-proxy caption of a real screenshot measures
  upstream elapsed; compare original vs detail=low vs downscaled.

### 3. Caption engine defaults to the turn model

- In `caption_images_in_messages`, pass `target_model` as the caption engine
  when `CODEX_IMAGE_MODEL` is unset. Keep `mimo-v2.5` as the fallback if the
  turn model rejects image input (4xx from upstream).
- Env override stays `CODEX_IMAGE_MODEL`; add `OPENCODE_GO_PROXY_CAPTION_MODEL=auto`
  as the explicit auto default.
- Verification: scratch-proxy run with default model deepseek-v4-flash;
  caption succeeds and is faster than mimo-v2.5.

### 4. Sane caption timeout, no caption retries

- Default `DEFAULT_CAPTION_TIMEOUT_SEC` 60 -> 30.
- Add a `max_retries` parameter to `call_upstream_chat` and pass 0 for
  caption calls. A failed caption degrades to the existing placeholder; it
  should not stack retries.

## Out of scope (do not touch)

- Main streaming request path, keepalive, meter, catalog, auth.
- `~/.codex/config.toml` (approval-gated; not needed for this fix).
- Local vision engines (Ollama etc.) - tracked separately in the wayfinder
  map (Vision bridge depth ticket).
- No new Python dependencies (stdlib only; `sips` is a macOS system tool).

## Test plan

- Unit: cache hit/miss/expiry/eviction; engine selection (env unset -> turn
  model, env set -> override); detail=low added; timeout default 30; caption
  retries disabled.
- Integration (`tests/test_integration.py`, existing MockUpstream pattern):
  same-image twice -> one upstream caption call; caption failure -> placeholder.
- Live (approval-gated): scratch proxy on port 8790 with the keychain key via
  the env-isolation recipe (`env -u OPENCODE_GO_API_KEY -u OPENCODE_API_KEY
  CODEX_KEYCHAIN_SERVICE=opencode-go-api-key ...`); time two consecutive
  identical captions; second must be < 100ms.

## Done criteria (machine-checkable)

- `uv run python -m pytest tests -q` green.
- Second identical screenshot caption returns from cache (< 100ms, no
  upstream call).
- Caption engine defaults to the turn model; mimo-v2.5 remains fallback.
- Caption timeout 30s, retries 0.

## Escape hatches

- If upstream rejects image input for the turn model (4xx), STOP, set the
  default back to mimo-v2.5 via env, and report the error body.
- If `detail: "low"` causes upstream errors, drop it and rely on downscale.

## Maintenance note

The wayfinder map's "Vision bridge depth" ticket (issue 14) wants the fuller
vision bridge (hash cache, engine auto-pick, metering, local engines). This
plan is the urgent subset; reuse its cache/engine structure so the later
bridge does not rewrite it. A stashed prototype (`git stash@{0}` in
/Users/user/opencode-go-proxy) already contains a vision.py with a 1h hash
cache and engine auto-pick - read it before writing new code.
