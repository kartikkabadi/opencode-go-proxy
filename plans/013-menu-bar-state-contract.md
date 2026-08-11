# Plan 013 - Menu bar Standard tier: local state contract + Swift consumption

## Why this matters

Ticket "Menu bar control panel scope" decided Standard tier: quota cards,
usage graph, provider row, single-port guard; vision panel and local-LLM
manager are out of 0.2.0. The Swift menu bar should read one stable local
state contract instead of scraping files.

## Current state

- macOS app in macos/ (Swift/AppKit) shows status/port/start-stop/logs/
  copy-port.
- /health exists; /quota does not yet exist (plan 011 adds it); meter writes
  usage-events.jsonl (plan 008 shape).

## Changes

1. Add GET /state (or extend /quota) returning one JSON contract:
   {status, port, upstream, quota: {limit, remaining, resetAt} | null,
   usage: {todayTurns, todayTokens, last7d: [tokens per day]},
   model: current model}. Compute from the meter file + quota state.
2. Update the Swift menu bar to render: status row, quota card (from quota),
   a small usage bar list (from usage), provider row; keep single-port
   guard and existing controls.
3. If the Swift build toolchain is unavailable on this machine, deliver the
   contract + a documented Swift diff and mark the Swift build as
   verified-by-docs.

## Out of scope

- Vision panel and local-LLM manager (out of 0.2.0).

## Verification gates

- uv run python -m pytest tests -q green for the /state endpoint;
- if Swift builds: macos app builds and shows the new panel.

## Escape hatches

If Xcode/Swift toolchain is missing, stop at the endpoint + documented
Swift changes and mark not-executed-here.
