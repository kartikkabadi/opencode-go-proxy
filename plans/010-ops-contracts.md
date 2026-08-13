STATUS: IMPLEMENTED on codex/0.2.0-parity-build (see plans/README.md and DEPLOYMENT.md). Historical planning doc; the shipped code is authoritative.

# Plan 010 - Ops contracts: smoke through proxy, support-bundle JSON v1, doctor depth, setup/install/status

## Why this matters

Tickets "Config and ops depth" and "Untested copies" decided: smoke-test
goes through the proxy with caller auth + marker; support-bundle matches the
reference JSON schema v1 (no tar.gz); doctor deepens toward the reference
check set with --fix; setup/install/status ship; base-URL overrides
(OPENCODE_GO_BASE_URL / OPENCODE_ZEN_BASE_URL) work.

## Current state

- ops.py: doctor (small check set), smoke-test (straight upstream), support
  bundle (tar.gz), keychain resolution now from secrets.py (plan 002).
- CLI: main() dispatches ops subcommands.

## Changes

1. smoke-test: POST through the local proxy (127.0.0.1:<port>/v1/responses)
   with a marker prompt, assert a completed response; report the failure
   path clearly. Keep an env override for a custom base.
2. support-bundle: emit JSON schema v1 (single JSON document with version,
   generatedAt ISO, env summary, config snapshot redacted, meter tail, log
   tail, catalog model count, doctor checks); redact secret-shaped values
   (existing regex); remove the tar.gz path.
3. doctor: add the missing reference-style checks (keychain resolution,
   catalog readable, config.toml presence, port free/owned, log writable,
   upstream reachable best-effort) with --fix where safe (no config.toml
   writes yet - those belong to the config-manager plan).
4. setup/install/status: install = copy the launchd plist into
   ~/Library/LaunchAgents + load it (macOS) with explicit confirmation flag;
   status = report running/port/log paths; uninstall/update/rollback are
   documented, not implemented (no approval to touch the live agent).
5. Base-URL overrides: config.chat_base_url honors --chat-base-url, then
   OPENCODE_GO_BASE_URL, then OPENCODE_ZEN_BASE_URL, then the legacy
   CHAT_COMPLETIONS_BASE_URL, then the built-in default (see
   config.resolve_chat_base_url).

## Out of scope

- No writes to the live launchd agent or config.toml in this plan; install
  is code + tests only, and running it is a gated deploy step.

## Verification gates

- uv run python -m pytest tests -q green; uvx ruff check src tests clean.
- support-bundle output parses as JSON with schema fields.

## Escape hatches

If --fix would write config.toml, defer that specific fix to the
config-manager plan and say so in the check hint.
