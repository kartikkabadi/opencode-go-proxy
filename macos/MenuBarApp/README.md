# OpenCode Go Menu Bar App

A native macOS menu bar app (Swift + AppKit, Swift Package Manager) that controls the
opencode-go-proxy Python bridge.

The menu bar shows a small branch icon (no long "opencode go/" text). The menu shows:

- status (Running / Stopped, with live health check against `/health`)
- port (default 8787)
- upstream base URL (from `GET /state`)
- Quota section: remaining/limit per provider plus reset countdown, or `n/a`
  when the upstream sent no rate-limit headers yet
- Today's turns and tokens, plus a 7-day token bar list (from `GET /state`)
- current model (the most recent meter event, else the proxy default)
- Start/Stop Proxy: launches `uvx --from git+... opencode-go-proxy` as a child process,
  writing logs to `~/.codex/logs/opencode-go-proxy.{log,err}` (same paths as the launchd plist)
- Open Logs / Reveal Log File
- Copy Port
- Quit (stops the child proxy first)

The panel reads one stable contract at `GET /state` (plan 013) instead of
scraping files; the row set is the Standard tier (quota card, usage bars,
provider row), with the vision panel and local-LLM manager out of scope for
0.2.0.

## Build

```bash
cd macos/MenuBarApp
swift build
```

Requires macOS 13+ and the Swift toolchain (`xcode-select --install`). This is a SwiftPM
executable target, so `swift build` works without an Xcode project. To bundle it as a .app:

```bash
swift build -c release
cp -R .build/release/OpenCodeGoMenuBar OpenCodeGoMenuBar.app/Contents/MacOS/
```

## Notes

- The Python bridge is untouched; the app only manages it as a child process.
- Single-port guard: the app refuses to Start if another process already listens on
  127.0.0.1:8787 (for example the launchd agent), instead of spawning a second proxy that
  would fail to bind. One proxy per port. To switch from launchd to the menu bar, stop the
  launchd agent first (`launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.opencode-go.proxy.plist`).
- The spawned proxy resolves the API key exactly as the CLI does: `$OPENCODE_GO_API_KEY`
  first, then `$OPENCODE_API_KEY`, then the macOS keychain services `opencode-go-api-key`
  and `codex-router-opencode-go`.
