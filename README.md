# DeepSeek Responses Proxy

[![CI](https://github.com/kartikkabadi/opencode-go-proxy/actions/workflows/ci.yml/badge.svg)](https://github.com/kartikkabadi/opencode-go-proxy/actions/workflows/ci.yml)

Local adapter for the awkward boundary between current Codex custom providers
and an upstream that speaks OpenAI-style Chat Completions. It ships with
DeepSeek V4 defaults because that was the first target that needed a bridge,
but the upstream URL and API-key source are configurable.

Codex `0.128.0` rejects `wire_api = "chat"` and expects Responses-shaped custom
providers. DeepSeek V4 officially exposes OpenAI Chat Completions and Anthropic
formats. This proxy owns that mismatch in one traceable local process:

```text
Codex /responses
  -> deepseek-responses-proxy
      -> DeepSeek /chat/completions
```

## Status

This is alpha infrastructure for local Codex custom-provider experiments. It is
known to work for DeepSeek V4 Pro and DeepSeek V4 Flash through Codex custom
providers, including the reasoning replay shape DeepSeek requires when a
thinking-mode response includes tool calls.

Implemented now:

- Responses `input` to chat `messages`
- `instructions` and `developer` mapped into system messages
- function tool schema passthrough where possible
- custom/freeform tool adaptation into `{ "input": "..." }` function tools
- DeepSeek `reasoning_content` replay across tool-call turns
- non-streaming JSON Responses output
- synthesized SSE for `stream: true`
- local health and model-list endpoints

Known limits:

- Hosted Responses tools are dropped. Function schemas are forwarded, and
  custom/freeform tools such as Codex's `apply_patch` are adapted into
  Chat-Completions function tools with an `input` string argument.
- SSE is synthesized after a non-streaming upstream call, so it is compatible
  with streaming clients but not token-realtime yet.
- The bridge follows the request shapes observed from Codex and DeepSeek V4; new
  Codex protocol changes should be captured as tests before widening behavior.

## Install

On Arch Linux, install the AUR package:

```bash
yay -S deepseek-responses-proxy-git
export DEEPSEEK_API_KEY="your-key-here"
systemctl --user enable --now deepseek-responses-proxy.service
systemctl --user status deepseek-responses-proxy.service
```

The AUR package installs the CLI as `/usr/bin/deepseek-responses-proxy` and a
user service at `/usr/lib/systemd/user/deepseek-responses-proxy.service`.

If your AUR helper has not picked up a fresh package index yet, install from the
AUR git repo directly:

```bash
git clone https://aur.archlinux.org/deepseek-responses-proxy-git.git
cd deepseek-responses-proxy-git
makepkg -si
```

No-package install path:

```bash
export DEEPSEEK_API_KEY="your-key-here"
uvx --from git+https://github.com/kartikkabadi/opencode-go-proxy deepseek-responses-proxy --help
```

Run the proxy without cloning:

```bash
uvx --from git+https://github.com/kartikkabadi/opencode-go-proxy \
  deepseek-responses-proxy \
  --bind 127.0.0.1 \
  --port 8787 \
  --chat-base-url https://api.deepseek.com
```

The key source order is:

1. `$DEEPSEEK_API_KEY` (or whatever `--api-key-env` names)
2. macOS keychain entry `opencode-go-api-key` (override with `CODEX_KEYCHAIN_SERVICE`)

Use `--api-key-env` and `--chat-base-url` to point the
same adapter at another OpenAI Chat Completions upstream.

The proxy accepts both `/responses` and `/v1/responses`.

From a development checkout:

```bash
uv sync
uv run deepseek-responses-proxy --help
```

## Systemd User Service

A ready-to-drop-in unit is included at
`contrib/systemd/deepseek-responses-proxy.service`. It runs the GitHub version
through `uvx`, binds only to localhost, and reads the upstream key from
`$DEEPSEEK_API_KEY`.

If you installed from AUR, use the packaged service instead:

```bash
systemctl --user enable --now deepseek-responses-proxy.service
systemctl --user status deepseek-responses-proxy.service
```

Install it as a user service:

```bash
mkdir -p ~/.config/systemd/user
cp contrib/systemd/deepseek-responses-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now deepseek-responses-proxy.service
systemctl --user status deepseek-responses-proxy.service
```

After PyPI publishing, the `ExecStart` command can be shortened from:

```text
/usr/bin/env uvx --from git+https://github.com/kartikkabadi/opencode-go-proxy deepseek-responses-proxy ...
```

to:

```text
/usr/bin/env uvx deepseek-responses-proxy ...
```

## Codex Config

Point Codex at the local Responses endpoint from `~/.codex/config.toml`:

```toml
model_catalog_json = "/home/you/.codex/deepseek-model-catalog.json"

[sandbox_workspace_write]
writable_roots = []
network_access = false
exclude_tmpdir_env_var = true
exclude_slash_tmp = true

[agents]
max_threads = 15
max_depth = 1

[model_providers.deepseek]
name = "DeepSeek"
base_url = "http://127.0.0.1:8787/v1"
experimental_bearer_token = "codex-deepseek-local"
wire_api = "responses"

[profiles.deepseek-v4-pro]
model_provider = "deepseek"
model = "deepseek-v4-pro"
model_context_window = 1000000
approval_policy = "untrusted"
sandbox_mode = "workspace-write"
features = { memories = false }

[profiles.deepseek-v4-flash]
model_provider = "deepseek"
model = "deepseek-v4-flash"
model_context_window = 1000000
approval_policy = "untrusted"
sandbox_mode = "workspace-write"
features = { memories = false }
```

The sandbox settings above are intentionally conservative for early testing:
allow edits inside the current workspace while requiring observation for broader
bash and tool use.

Start Codex with the profile, not just the model flag:

```bash
codex -p deepseek-v4-pro
codex -p deepseek-v4-flash
```

`codex -m deepseek-v4-pro` changes only the model name. It leaves the provider
on the default OpenAI/ChatGPT path and can produce a misleading "model is not
supported when using Codex with a ChatGPT account" error.

### Model Metadata

Without a catalog entry, Codex prints this warning every turn:

```text
Model metadata for `deepseek-v4-pro` not found. Defaulting to fallback metadata; this can degrade performance and cause issues.
```

`model_context_window = 1000000` is not enough to make the model known; it only
overrides limits after model lookup. To suppress the warning, provide
`model_catalog_json` with entries for `deepseek-v4-pro` and
`deepseek-v4-flash`.

The safest local pattern is to copy Codex's bundled `models.json` for your
installed Codex version and append the DeepSeek entries, using an existing Codex
model as the template so `base_instructions` stays intact. The resulting JSON
must have the shape:

```json
{
  "models": [
    {
      "slug": "deepseek-v4-pro",
      "display_name": "DeepSeek V4 Pro",
      "context_window": 1000000,
      "max_context_window": 1000000,
      "supported_in_api": true,
      "base_instructions": "..."
    }
  ]
}
```

Keep the full model object fields from the Codex template; the snippet above is
only the identifying part.

For `apply_patch_tool_type`, both Codex values are usable through this bridge:
`freeform` is adapted into a Chat-Completions function tool at the proxy
boundary, while `function` is already native to the upstream Chat shape. Keeping
Codex's normal `freeform` setting is fine.

## Codex Agents

Codex sub-agents can be backed by the same provider. Put these files under
`~/.codex/agents/` after adding the provider/profile config above.

### `~/.codex/agents/deepseek-pro-worker.toml`

```toml
name = "deepseek-pro-worker"
description = """
DeepSeek V4 Pro cwd-sandboxed sub-agent for substantial review, debugging,
architecture analysis, long-context scouting, and bounded local edits. Uses Codex's Responses
custom-provider path via the local deepseek-responses-proxy shim, which
translates to DeepSeek Chat Completions.
Prefer this agent when the work needs stronger reasoning or long-context
synthesis and can tolerate an experimental provider bridge.

REQUIRES FULL BRIEFING. Each spawn starts a fresh thread with zero inheritance
from your conversation. The sub-agent sees only your message, its
developer_instructions, repo AGENTS.md/CLAUDE.md, and files it reads itself.

Because this rides a local protocol bridge, treat MCP/native subagent behavior
as experimental. It is intentionally cwd-sandboxed: file edits are allowed in
the working directory, while broader shell/tool access must be observed and
approved.
"""
nickname_candidates = [
  "deepseek-pro", "v4-pro", "longctx", "sparse", "router", "meridian",
  "indexer", "synthesist", "cartographer", "resolver", "compiler",
  "surveyor", "weaver", "planner", "auditor", "fixpoint",
]

model = "deepseek-v4-pro"
model_provider = "deepseek"
model_context_window = 1000000
model_reasoning_effort = "high"

approval_policy = "untrusted"
sandbox_mode = "workspace-write"

developer_instructions = """You are a DeepSeek V4 Pro worker sub-Codex agent.

# Role

You are a cwd-sandboxed production worker for substantial coding tasks:
debugging, code review, bounded local edits, refactoring plans, architecture
analysis, and long-context synthesis. Infer the exact role from the parent
agent's briefing and execute it inside the current working directory.

# Context boundary

You start fresh. You do not inherit the parent conversation, previous file
reads, plans, or decisions. The spawn message is your complete handoff. If the
brief lacks a goal, concrete file paths, known constraints, or done criteria,
return a concise blocking question instead of guessing.

# Provider boundary

Your model is DeepSeek V4 Pro through a local Responses-to-ChatCompletions
bridge. Treat native Responses-only affordances as possibly unavailable or
weaker than OpenAI-hosted models. Prefer direct file reads, shell checks, and
focused patches over depending on complex tool choreography.

# Work contract

Only edit files in the current working directory. Do not request broader
permissions unless the parent explicitly asks you to escalate for a bounded
operation. Respect concurrent agents. Return:

- what you changed or found
- exact files inspected or changed
- what you verified
- any residual risk or blocked follow-up
"""
```

### `~/.codex/agents/deepseek-flash-worker.toml`

```toml
name = "deepseek-flash-worker"
description = """
DeepSeek V4 Flash cwd-sandboxed sub-agent for cheap parallel scouting, focused
file inspection, bounded local edits, and fast second opinions. Uses Codex's
Responses custom-provider path via the local deepseek-responses-proxy shim,
which translates to DeepSeek Chat Completions.

Best for bounded tasks with crisp inputs: inspect these files, summarize this
flow, make this cwd-local patch, check this hypothesis, or review this narrow diff. Escalate to
deepseek-pro-worker or claude when ambiguity, architecture, or high-risk editing
dominates.
"""
nickname_candidates = [
  "deepseek-flash", "v4-flash", "spark", "scout", "probe", "sketch",
  "runner", "tracer", "mapper", "needle", "delta", "lint", "pass",
  "quickcheck", "sidecar", "finder",
]

model = "deepseek-v4-flash"
model_provider = "deepseek"
model_context_window = 1000000
model_reasoning_effort = "medium"

approval_policy = "untrusted"
sandbox_mode = "workspace-write"

developer_instructions = """You are a DeepSeek V4 Flash worker sub-Codex agent.

# Role

You are a fast cwd-sandboxed sidecar worker for bounded tasks: repository
scouting, focused file inspection, small local patches, lightweight review, and
hypothesis checks. Optimize for short, concrete output that the parent can
immediately weave into the main task.

# Context boundary

You start fresh. You do not inherit the parent conversation, previous file
reads, plans, or decisions. The spawn message is your complete handoff. If the
brief is underspecified, say exactly what is missing and stop.

# Provider boundary

Your model is DeepSeek V4 Flash through a local Responses-to-ChatCompletions
bridge. Avoid elaborate tool chains when a simple file read, search, or patch is
enough. If tool behavior looks degraded, report the symptom plainly.

# Work contract

Only edit files in the current working directory. Do not request broader
permissions unless the parent explicitly asks you to escalate for a bounded
operation. Return:

- concise finding or patch summary
- exact files inspected or changed
- verification performed
- next action if the task should continue elsewhere
"""
```

## Trace

Every request emits compact JSON lines on stderr.

Important trace events:

- `server.start`
- `request.received`
- `request.converted`
- `credential.source`
- `upstream.start`
- `upstream.done`
- `response.converted`
- `request.failed`

## Development

```bash
uv run python -m unittest discover -s tests -v
uvx ruff check
uv build
```

## Publishing

See [PUBLISHING.md](PUBLISHING.md). Licensed under [MIT](LICENSE).
