# Security

`opencode-go-proxy` is intended to run as a local adapter between Codex
and an upstream Chat Completions API.

## Secrets

The proxy never needs credentials committed to the repository. It resolves the
upstream API key in this order:

1. The configured environment variable, defaulting to `OPENCODE_GO_API_KEY`.
2. The macOS keychain entry `opencode-go-api-key` (override with `CODEX_KEYCHAIN_SERVICE`).

## Network exposure

Bind to `127.0.0.1` unless you have a deliberate reason to expose the proxy.
The proxy emits a `security.warning` trace when bound to a non-localhost address.
Codex should talk to the local `/v1/responses` endpoint, and the proxy should
be the only process that talks to the upstream API with the real provider key.

## SSRF protection

Image URLs in conversation content are validated — only `data:image/` and
`https://` schemes are allowed. `file://`, `http://`, `ftp://`, and other
schemes are rejected to prevent server-side request forgery.

## Reports

Open a private security advisory or contact the maintainers before publishing a
bug report that includes credentials, prompts, tool outputs, or request traces.
