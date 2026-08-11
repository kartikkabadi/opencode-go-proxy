# Plan 007 - Correctness contract: empty-completion retry, zero-token estimate, keepalive to end

## Why this matters

Ticket "Correctness and reliability contract" decided three behaviors that
are not yet implemented:

1. Empty-completion: a streamed 200 with no text, no tool call, and no
   reasoning is retried ONCE with the identical request; terminal events are
   held on the first attempt; if the retry is also empty, emit an error
   event with code empty_completion. Reuse item ids across attempts so the
   client never sees ghost items.
2. Zero-input-token estimation: replace an explicit numeric input_tokens: 0
   with ceil(bytes / 3.3), floor 1000, capped at the model context window,
   kept separate from provider-reported usage, self-disabling when upstream
   reports non-zero again, with an env kill switch.
3. Keepalive runs until the stream truly ends (today it stops at the first
   upstream byte), with serialized socket writes so comments cannot
   interleave into data frames, and stops on every exit path.

## Current state

- streaming.py: keepalive_stop.set() once upstream starts responding
  (council finding still present); no empty-completion retry; usage tokens
  pass through normalize_usage verbatim.

## Changes (streaming.py + upstream.py/meter as needed)

### 1. Empty-completion retry

- In the streaming engine, track empty (no text, no tool_calls, no
  reasoning) at stream end. If empty and this is the first attempt, hold the
  terminal events (response.completed / output_item.done), re-run the
  identical upstream request once, reusing response_id and item ids. If the
  retry produces content, finalize normally. If the retry is also empty,
  emit {"type":"response.error","error":{"code":"empty_completion",
  "message":"upstream returned an empty completion"}} then [DONE], and meter
  the turn with empty_completion=True and retries=1.
- Only retry once; never for client aborts or upstream failures.

### 2. Zero-input-token estimation

- In normalize_usage or the meter call site: when input_tokens is exactly 0,
  estimate = max(1000, ceil(prompt bytes / 3.3)), capped at the model's
  context window (from known_models() or a sane default 272000). Keep it in a
  separate field (estimatedInputTokens) when provider reports 0, and stop
  substituting once upstream reports non-zero. Kill switch:
  OPENCODE_GO_PROXY_ESTIMATE_ZERO_INPUT=0 disables substitution.

### 3. Keepalive to true end

- Keep the keepalive thread running until the stream truly ends (upstream
  EOF, client disconnect, error, or finalize). Serialize wfile writes with a
  lock shared with the event writer so comments never interleave into data
  frames. Ensure every exit path sets the stop event.

## Out of scope

- No config.toml writes. No changes to non-streaming calls.

## Verification gates

- uv run python -m pytest tests -q green.
- uvx ruff check src tests clean.
- New tests: empty stream retried once (upstream call count == 2), retry
  produces content (no error event), retry also empty (error event with
  empty_completion), zero-input estimation math + kill switch, keepalive
  thread alive through a mid-stream stall and stopped on exit.

## Test plan

- tests/test_correctness.py (or extend test_streaming.py): the above, using
  the MockUpstream pattern.

## Done criteria

All gates pass; a silently-empty upstream never leaves the client with a
successful-looking empty turn; zero-token turns still trigger compaction;
mid-stream stalls keep the client alive.

## Escape hatches

If re-running the identical request mid-stream proves unsafe (e.g. the
upstream consumes a side effect), STOP and report instead of weakening the
retry.
