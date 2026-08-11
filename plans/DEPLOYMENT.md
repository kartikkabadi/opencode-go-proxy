# Deployment handoff: 0.2.0 parity + architecture build

Branch: codex/0.2.0-parity-build (origin, pushed). Base: 9bb411d
(== origin/proxy/skills). Build: plans 001-013 + 009b, all reviewed,
tested (405 passing), ruff clean, live e2e verified on scratch ports.

## What changed (commit chain)

5d762cc plan 001 vision caption latency (cache, detail low + sips, turn-model engine, 30s no retry)
2617ac1 plan 002 lint clean
f175f64 plan 002 module split (secrets, upstream, streaming, thin app.py)
b4ad017 plan 003 catalog contract + overlay + runtime reload
61ee34a plan 004 vision bridge (evidence, adapters, auto-pick, metered)
f71435d plan 005 chat/completions passthrough + /messages 400 + WS 426 test
de6c30c plan 006 auth transport guard
1885f67 plan 007 empty-completion retry, zero-token estimate, keepalive to end
b96d0e2 plan 008 meter canonical schema (ISO, camelCase, meteringVersion, provider)
af24ed5 plan 009 session-model injection align, discovery UA, image-capable routing
4ec678d plan 010 ops contracts (smoke through proxy, bundle JSON v1, doctor, install/status)
25175bc plan 011 rate-limit harvesting + /quota
fd2912b plan 012 config manager (marker block, refuse-to-clobber, Voice preservation)
ddce6d7 plan 013 menu bar /state contract + Swift Standard tier
6093628 plan 009b runtime image fallback (live e2e found: upstream rejects image_url for
        deepseek; non-tools image turns now caption-and-retry via mimo)

## Gated deploy steps (need Kartik's explicit approval, one at a time)

1. Swap the live proxy code (restarts the running proxy on 8787):
   cd ~/Documents/Codex/2026-08-10/re/work/opencode-go-proxy
   git fetch origin codex/0.2.0-parity-build
   git checkout live/0.2.0-fixed
   git merge --ff-only FETCH_HEAD   # or: git reset --hard origin/codex/0.2.0-parity-build
   launchctl kickstart -k gui/$(id -u)/com.opencode.go.proxy
   Verify: curl -s http://127.0.0.1:8787/health ; curl -s http://127.0.0.1:8787/v1/models | wc -c
2. Managed config (writes ~/.codex/config.toml, approval-gated):
   uv run python -m opencode_go_proxy config status   # dry look first
   uv run python -m opencode_go_proxy config enable   # marker block; Voice keys preserved; refuses to clobber user values
3. Optional: install the updated launchd plist (state-dir catalog env):
   uv run python -m opencode_go_proxy install --yes  # copies contrib plist + launchctl load
4. Post-swap checks: /health, /state, /quota, one /v1/responses turn, one image turn.

## Rollback

Old code is at live/0.2.0-fixed HEAD 9bb411d. To revert:
   cd ~/Documents/Codex/2026-08-10/re/work/opencode-go-proxy
   git checkout live/0.2.0-fixed && git reset --hard 9bb411d
   launchctl kickstart -k gui/$(id -u)/com.opencode.go.proxy
Run `opencode-go-proxy config disable` if the managed block was enabled.

## Notes

- Meter schema changed (plan 008): new events are ISO/camelCase; historical
  events in ~/.codex/opencode-go-proxy/usage-events.jsonl are legacy
  snake_case/epoch. state.py parses both.
- The live meter file was touched during this build by a subagent mistake
  (21 branch-schema events); they were removed and the file restored to
  legacy-only (verified: 0 meteringVersion lines remain).
- models.dev discovery now sends a UA (was 403); /v1/models serves 91 models.
- Config.toml was NOT modified by this build.
