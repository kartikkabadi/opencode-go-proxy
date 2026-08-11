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

### Step 5 - Review-driven fixes, batch 4 (config/catalog/protocol/state/vision/menu bar)

- config_manager: enable no longer duplicates openai_base_url/model_catalog_json
  when user-owned values already match (P1; TOML stays valid).
- catalog.py: load_known_slugs tolerates a non-object catalog root (P2);
  empty announcement state is never persisted so first discovery does not
  announce the whole catalog (P2); offline first-run refresh falls back to
  the seed instead of raising (P2); dead OPTIONAL_MODEL_KEYS constant removed.
- protocol.py: shared _catalog_mtime() helper for the three mtime-cached
  catalog readers; reload_known_models clears all three caches (P3).
- state.py: /state counts zero-token-estimated turns via estimatedInputTokens
  (P2); malformed sampledAt snapshots are skipped instead of winning (P2).
- vision.py: local runtime probe requires the configured model to be served
  (P2, no captions against nonexistent models); dead caption_image removed.
- Menu bar Swift: /state fetch retains the last good state on transient
  failure and ignores stale callbacks after the child PID changes (P2);
  swift build verified.

Verification: 454 passed locally, ruff clean, swift build complete, live e2e
on 8790 (health, 91 models, /state, responses turn, chat passthrough 200).
Commit: (next)

### Step 6 - Review-driven fixes, batch 5 (tests + docs + startup validation)

- app.py: startup validates --timeout-sec is positive (P2).
- tests: config_manager assertion uses managed_base_url(); protocol env
  override uses a distinct vision slug; secrets drops the dead double patch;
  state /state port contract uses the real bound port; image-fallback
  streaming test asserts the retry body has no image_url; env-matrix state
  dir uses the constant; concurrency tests isolated earlier.
- CHANGELOG caption-engine wording corrected (auto picks cheapest image-capable
  catalog model, not the turn model).
- plans/: README table regenerated for all 13 plans + 009b with implemented
  statuses; every plan file got a STATUS banner marking it historical so
  stale line refs cannot mislead a future executor.

Verification: 454 passed locally, ruff clean. Commit: (next)

### Step 7 - Architecture pass (improve-codebase-architecture)

Report: $TMPDIR/architecture-review-20260811-112434.html (opened).

- A4: app.py handlers use one _config() accessor (removes 3 type-ignore dups).
- A2: deleted dead resolve_caption_model(); the engine-resolution contract
  tests now pin resolve_engines (the real seam).
- A1: extracted _open_upstream_stream() + _ConnectFailed in streaming.py; the
  responses engine and the chat passthrough now share one connect-with-retry
  seam (their terminal handling stays distinct).
- A3 (protocol.py split) documented as a candidate, not executed: no current
  pain, characterization tests exist.

Verification: 454 passed locally, ruff clean, live e2e on 8790 (health,
responses stream, chat passthrough stream, responses turn). Commit: (next)

### Step 8 - PR stack restructure (all PRs)

Kartik: base must be main, work must be stacked PRs, applies to all PRs.
The stack was linearized bottom-up (rebases + force-pushes, no content loss):

main <- PR3 catalog <- PR4 relay <- PR5 reliability <- PR7 ops <-
PR6 menu <- PR8 skills <- PR21 build

- All 7 PRs now have green `test` checks (hermetic catalog/state-dir conftest,
  shape-based /v1/models assertion, ruff clean on every branch).
- All 7 PRs report CLEAN merge state; mergeable bottom-up.
- PR 21 rebased onto the linearized skills tip (454 tests green, ruff clean).
- Commits per branch: catalog e2679e1+3 fixes, relay 67b0a1e, reliability
  8c18327, ops b8a0f93, menu 51f3227, skills 3c347ed, build ee79c8e.
