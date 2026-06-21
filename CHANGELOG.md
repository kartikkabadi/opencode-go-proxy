# Changelog

## [0.1.2] - 2026-06-21

Service file consistency + PKGBUILD fix.

### Fixed

- systemd service now sets `CODEX_MODEL_CATALOG` and `OPENCODE_GO_PROXY_USER_AGENT` (matches launchd plist)
- PKGBUILD `check()` uses `uv run python -m pytest tests` (was `PYTHONPATH=src python -m pytest` — fails in clean chroot without pytest)
- PKGBUILD `package()` installs catalog to `/usr/share/opencode-go-proxy/opencode-go-catalog.json` and seds service to point there
- AUR install script: added catalog note, fixed step numbering

## [0.1.1] - 2026-06-21

Graceful shutdown + launchd service file.

### Added

- SIGTERM handler for graceful shutdown on `launchctl bootout` / `systemctl stop`
- launchd plist at `contrib/launchd/com.opencode-go.proxy.plist`
- README: launchd setup instructions with copy + bootstrap commands

## [0.1.0] - 2026-06-21

Initial public release.

### Added

- Responses `input` to chat `messages` translation
- `instructions` and `developer` roles mapped to system messages
- Function tool schema passthrough
- Custom/freeform tool adaptation (Codex `apply_patch` works)
- `reasoning_content` replay across tool-call turns
- Real-time SSE streaming
- Image captioning via MiMo V2.5 when tools are present
- SSRF protection on image URLs (`data:image/` and `https://` only)
- Configurable body cap, bind address guard
- macOS keychain credential resolution
- Local health and model-list endpoints
- Reference model catalog with all 13 OpenCode Go models
- AUR package (`opencode-go-proxy-git`)
- systemd user service
- 40 tests (unit + integration) covering protocol, credentials, HTTP round-trip, alias map, tool calls, streaming tool calls, streaming error handling, streaming crash recovery, SSRF, and image captioning

### Security

- SSRF validation on image URLs
- Non-negative Content-Length validation
- Generic error messages to client (full bodies only in trace logs)
- No path reflection in 404 responses
- Bind address guard warns on non-localhost
