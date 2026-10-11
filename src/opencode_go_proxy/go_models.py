"""Certified OpenCode Go model-to-protocol routing.

The public Go catalog can change independently of the wire contract. A model
is only exposed dynamically after its Chat, Messages, or Responses family has
been documented or otherwise verified.
"""

from __future__ import annotations

from typing import Final

OPENAI_RESPONSES: Final = "openai_responses"
OPENAI_CHAT: Final = "openai_chat"
ANTHROPIC_MESSAGES: Final = "anthropic_messages"

DOCUMENTED_RESPONSES_MODELS: Final = frozenset(
    {
        "grok-4.6",
        "gpt-5.6-luna",
        "muse-spark-1.3-contributor",
        "muse-spark-1.2-contributor",
    }
)

DOCUMENTED_CHAT_MODELS: Final = frozenset(
    {
        "glm-5.3-flash",
        "glm-5.3",
        "glm-5.2",
        "glm-5.1",
        "kimi-k3",
        "kimi-k2.7-code",
        "kimi-k2.6",
        "longcat-2.0",
        "longcat-2.5-preview-free",
        "deepseek-v4.1-flash",
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "deepseek-v4-flash-vision-exp",
        "mimo-v2.5",
        "mimo-v2.5-pro",
        "mimo-v2.6-flash",
        "hy4-preview",
        "hy3",
    }
)

DOCUMENTED_MESSAGES_MODELS: Final = frozenset(
    {
        "minimax-m3",
        "minimax-m2.7",
        "minimax-m2.5",
        "qwen3.8-max",
        "qwen3.8-flash",
        "qwen3.7-max",
        "qwen3.7-plus",
        "qwen3.6-plus",
    }
)

DOCUMENTED_GO_MODELS: Final = (
    DOCUMENTED_RESPONSES_MODELS
    | DOCUMENTED_CHAT_MODELS
    | DOCUMENTED_MESSAGES_MODELS
)

# These IDs remain present in the live /models catalog and have a verified
# protocol family, but are not part of the current public documentation table.
VERIFIED_LEGACY_FAMILIES: Final = {
    "grok-4.5": OPENAI_RESPONSES,
    "glm-5": OPENAI_CHAT,
    "kimi-k2.5": OPENAI_CHAT,
    "qwen3.5-plus": OPENAI_CHAT,
}

GO_MODEL_FAMILIES: Final = {
    **{model: OPENAI_RESPONSES for model in DOCUMENTED_RESPONSES_MODELS},
    **{model: OPENAI_CHAT for model in DOCUMENTED_CHAT_MODELS},
    **{model: ANTHROPIC_MESSAGES for model in DOCUMENTED_MESSAGES_MODELS},
    **VERIFIED_LEGACY_FAMILIES,
}


def go_family_for(model: str) -> str:
    """Return the certified upstream protocol family for a bare Go model."""
    return GO_MODEL_FAMILIES[model]


def certified_go_models() -> set[str]:
    """Return all Go IDs whose upstream protocol is known."""
    return set(GO_MODEL_FAMILIES)
