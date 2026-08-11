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
