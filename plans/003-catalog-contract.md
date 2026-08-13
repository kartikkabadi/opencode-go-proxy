STATUS: IMPLEMENTED on codex/0.2.0-parity-build (see plans/README.md and DEPLOYMENT.md). Historical planning doc; the shipped code is authoritative.

# Plan 003 - Catalog contract with overlay + runtime reload

## Why this matters

Custom models must work by definition, not by patching. Today the full-shape
renderer hand-copies fields and drops real keys (comp_hash, token_budget,
approvals, collaboration_modes, auto_review, permissions) and puts
multi_agent_version inside model_messages instead of at model top level.
KNOWN_MODELS is frozen at import, so a refresh cannot affect the running
process, and refresh writes a CWD-relative contrib path.

## Current state

- `src/opencode_go_proxy/catalog.py:210` `render_full_catalog`: field-copy
  projection with dropped keys and misplaced multi_agent_version.
- `src/opencode_go_proxy/protocol.py:146` KNOWN_MODELS frozen at import.
- `catalog.py` refresh: additive auto-merge from models.dev (providerID
  opencode), writes CWD-relative contrib files.
- Reference: codex-router `catalog.mjs` merged shape and
  `~/.codex/codex-router/merged-models.json` (key set to match).

## Changes

### 1. Canonical model contract

- Define the canonical model object shape in catalog.py as a documented
  dict with the exact merged key set. multi_agent_version and comp_hash live
  at model top level; token_budget, approvals, collaboration_modes,
  auto_review, and permissions live inside model_messages (MODEL_MESSAGES_KEYS),
  matching the shipped catalog.py contract and codex-router's merged shape.
- Rebuild `render_full_catalog` as a projection FROM this canonical shape,
  with no hand-copied drop list.

### 2. Runtime reload

- Replace the frozen KNOWN_MODELS with `known_models()` cached by file
  mtime and `reload_known_models()`; update protocol.py call sites and
  `/v1/models` to use the live set.
- Startup refresh must not block on a 10s network fetch outside the repo
  root: load the state-dir compact first, refresh in the background.

### 3. Overlay for custom models

- After the merge, apply user overrides from user-models.json (add, hide,
  edit display) as an overlay, then announcements (availability_nux) and
  hidden-model flags.
- refresh writes the runtime compact only under the state dir
  (OPENCODE_GO_PROXY_STATE_DIR, default ~/.codex/opencode-go-proxy/).

## Out of scope

- No config.toml writes. No multi-provider routing. No curation UI.
- models.dev stays the discovery source for the official catalog.

## Verification gates

- New key-set parity test compares the renderer output to a fixture sampled
  from merged-models.json; green.
- Refresh writes nothing outside the state dir (assert no contrib writes to
  CWD).
- After a refresh with a curated model added, `/v1/models` grows without a
  restart.

## Test plan

- `tests/test_catalog_render.py`: parity key set, multi_agent_version at top
  level, visibility/availability flags.
- `tests/test_catalog_local.py`: overlay merge (add/hide/edit), reload after
  refresh, state-dir write confinement.

## Done criteria

Parity test green; suite green; reload works without restart; writes
confined to the state dir.

## Escape hatches

If the merged-models.json fixture contains fields the proxy does not need,
keep them in the canonical shape but mark the optional set explicitly; do
not silently drop them.
