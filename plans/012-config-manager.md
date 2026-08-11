STATUS: IMPLEMENTED on codex/0.2.0-parity-build (see plans/README.md and DEPLOYMENT.md). Historical planning doc; the shipped code is authoritative.

# Plan 012 - Config manager: marker-block config.toml, refuse-to-clobber, Voice preservation

## Why this matters

Tickets "Protocol surface and Voice handling" and "Config and ops depth"
decided the proxy manages config.toml via marker blocks: writes
openai_base_url + model_catalog_json under BEGIN/END marker comments,
refuses to clobber user-owned values, preserves Codex Voice realtime keys
(experimental_realtime_webrtc_call_base_url / experimental_realtime_ws_base_url
on native endpoints), and exposes enable/disable/status.

## Current state

- config.toml is hand-edited; the proxy never touches it.
- A prototype config manager exists in stash@{0} (config.py) built by an
  earlier session; use it as a reference only.

## Changes (new src/opencode_go_proxy/config_manager.py + CLI)

1. config enable: atomically insert the managed block into
   ~/.codex/config.toml with the proxy base URL and the catalog path; never
   overwrite existing user values for openai_base_url/model_catalog_json
   (refuse-to-clobber: if user-owned values exist outside the block, fail
   with a clear message or skip and report).
2. Voice preservation: ensure the realtime keys stay on native endpoints;
   the manager never removes or overrides them.
3. config disable: remove only the managed block; leave the file otherwise
   untouched; remove the file if the block was the only content.
4. config status: show managed state (enabled/disabled, block present, user
   keys untouched).
5. Tests use a temp config file via env (CONFIG_PATH override or a
   config-manager test hook); never touch the real config.toml.
6. CLI: opencode-go-proxy config enable|disable|status.

## Out of scope

- Running enable against the real ~/.codex/config.toml is a gated deploy
  step, not part of this plan.

## Verification gates

- uv run python -m pytest tests -q green; uvx ruff check src tests clean.
- Round-trip test on a temp file: enable -> block present, user values
  preserved, Voice keys intact; disable -> block gone, file unchanged
  otherwise.

## Escape hatches

If config.toml parse is ambiguous (multiple blocks, foreign router blocks),
report instead of writing; never guess.
