# Cleanup progress: CI green + clean-code + architecture refactor

Branch: codex/0.2.0-parity-build (PR #21). Goal: fix failing checks, then
clean and refactor the codebase until the architecture is sound, verifying
and committing after each significant step.

Method per step: implement -> full suite + ruff -> live-test on a scratch
proxy (port 8790, keychain key, temp state dir) when behavior is involved ->
autoreview the diff -> commit with a clear message -> update this file.

## Step log

### Step 1 - Fix CI failures (Linux runner)

Evidence: PR #21 test job failed with 5 failures on the Linux runner.

- tests/test_env_matrix.py: caption-model assertions assumed auto-pick
  returned the turn model; the current contract is env override ->
  auto-pick cheapest image-capable catalog model -> mimo-v2.5 fallback.
  Rewrote as deterministic tests: explicit pin, CODEX_IMAGE_MODEL override,
  auto-pick with a patched catalog, fallback with an empty catalog.
- tests/test_ops.py: install tests exercised the macOS launchd path without
  mocking platform.system; on Linux install returns 1 ("macOS-only").
  Patched platform.system -> Darwin in the three macOS-path tests.

Verification: uv run python -m pytest tests -q -> 439 passed locally.
Commit: (next)

### Review-comment audit (PR #21 + stack PRs)

Collectors pulled all comments: cubic ran 3 passes on PR 21 (12 + 35 + 5
issues), CodeRabbit skipped (base-branch reviews disabled), Socket passes,
chatgpt-codex-connector left P1/P2 notes. Stack PRs 3-8 all fail CI on one
stale test (test_models_endpoint expecting deepseek-v4-flash in an empty
models list); PR 21's catalog seeding fixes the underlying case when merged.

Triage of real, actionable findings (P1 first):
- P1 quota OverflowError on malformed reset header
- P1 support-bundle leaks bearer creds (skips redaction for config contents)
- P1 install writes launchd plist with literal %h paths
- P1 streaming mixed text-and-tool output_index collision
- P1 config_manager duplicate TOML keys on enable
- P1 models_cmd hide/show erases other overlay fields
- P2 test_concurrency writes usage events to the real default state dir
- P2 trace writes unguarded -> concurrent stderr JSONL corruption
- P2 WS upgrade with Origin answered 403 before the 426 reject
- P2 app.py image-fallback turn records retries=0 despite a second attempt
- P2 chat passthrough non-stream always labels application/json
- P2 negative upstream timeout crashes requests
- P2 vision auto-local can nominate a runtime with a missing model
- P2 state.py under-counts zero-token-estimated turns; malformed sampledAt
- P2 protocol.py invalid catalog root raises AttributeError; 3x cache scaffold
- P2 catalog first-launch announces all models; offline first-run raises
- P2 ops.py install crash on unwritable dir; doctor config/port checks
- P2 menu bar stale state on transient /state failure and on child stop
- P3 test robustness (cache timing, meter path, dead mocks, conflation) +
  docs drift (DEPLOYMENT label, plans stale, CHANGELOG wording)

### Step 2 - Review-driven fixes, batch 1 (data safety + curation + test isolation)

- quota.py: guard reset parsing against OverflowError (huge numbers,
  unrepresentable epochs); tests added.
- models_cmd.py: hide/show now amend the existing overlay entry instead of
  replacing it; --set coerces int/float fields (context_window, priority);
  removed the dead JSON_DOC placeholder.
- test_concurrency.py: autouse fixture isolates OPENCODE_GO_PROXY_STATE_DIR
  to a temp dir (was writing to the real default state dir).
- test_env_matrix.py: state_dir default asserted against meter.DEFAULT_STATE_DIR.

Verification: 443 passed locally. Commit: (next)

### Step 3 - Review-driven fixes, batch 2 (streaming/app correctness)

- streaming.py: output_index is now a monotonic counter assigned once per
  output_item.added and reused by every delta/done/completed; mixed text and
  tool calls can no longer share an index (P1, regression test added).
- trace.py: module-level lock serializes each stderr JSONL record (P2).
- app.py do_GET: WebSocket upgrades answer 426 before the auth guard, so a
  realtime handshake carrying Origin is no longer 403 (P2; live-verified).
- app.py: failed image-fallback turns now meter retries=(exc.retries+1) (P3).
- app.py + upstream.py: verbatim chat passthrough relays the upstream
  content-type instead of always labeling application/json (P3).
- streaming.py: mid-stream upstream read failures in the chat passthrough
  terminate the relay without rendering a second HTTP response (P2).
- streaming.py: dropped the never-True empty flag and the emptyCompletion
  marker on 502 no-data streams (empty completion stays a 200-only concept).

Verification: 444 passed locally; live e2e on 8790: health 200, WS+Origin
426, chat passthrough relays upstream content-type, stream ends with [DONE].
Commit: (next)

### Step 4 - Review-driven fixes, batch 3 (ops security + install + doctor)

- ops.py support-bundle: config snapshot now runs the full redactor (bearer,
  sk- keys, private keys), not just key=value patterns (P1).
- ops.py install: renders the launchd plist with the real home directory
  instead of literal %h paths (P1), and computes the target path before the
  write block so a failure reports cleanly (P2). Tests added.
- ops.py doctor: check_config parses the root openai_base_url assignment
  instead of grepping the whole file (P2); check_port probes the socket after
  a failed health request to detect non-HTTP occupants (P2). Tests added.
- plans/DEPLOYMENT.md: kickstart label corrected to com.opencode-go.proxy.

Verification: 448 passed locally. Commit: (next)
