"""Code-owned OpenAI-compatible Chat dialect registrations.

This module declares which optional Chat Completions features DrowAI may send
to a reviewed compatible route. It contains no credentials, endpoints,
transport behavior, provider-specific branches, or user-configurable policy.
"""

from __future__ import annotations

from ...contracts.compat import LLMDialectPolicy
from ...core.capabilities import LLMCapability
from ...core.exceptions import LLMConfigurationError


OPENAI_COMPATIBLE_CHAT_ADAPTER_ID = "openai_compatible_chat"
OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION = "1"

_ADAPTER_CAPABILITY_CEILING = frozenset(
    {
        LLMCapability.CHAT,
        LLMCapability.STREAMING,
        LLMCapability.TOOLS,
        LLMCapability.STRUCTURED_OUTPUT_NATIVE,
        LLMCapability.USAGE_REPORTING,
        LLMCapability.STREAMING_USAGE_REPORTING,
        LLMCapability.CONTEXT_WINDOW,
        LLMCapability.MAX_OUTPUT_TOKENS,
    }
)
_ADAPTER_TOOL_CHOICE_MODES = frozenset({"auto", "required"})
_ADAPTER_STRUCTURED_OUTPUT_STRATEGIES = frozenset({"native_schema"})

CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT = LLMDialectPolicy(
    policy_id="openai_compatible_chat.conservative_v1",
    adapter_id=OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
    api_surface="chat_completions",
    capabilities=frozenset(
        {
            LLMCapability.CHAT,
            LLMCapability.STREAMING,
            LLMCapability.USAGE_REPORTING,
        }
    ),
    max_retry_attempts=2,
)

AGENT_OPENAI_COMPATIBLE_DIALECT = LLMDialectPolicy(
    policy_id="openai_compatible_chat.agent_v1",
    adapter_id=OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
    api_surface="chat_completions",
    capabilities=frozenset(
        {
            LLMCapability.CHAT,
            LLMCapability.STREAMING,
            LLMCapability.TOOLS,
            LLMCapability.STRUCTURED_OUTPUT_NATIVE,
            LLMCapability.USAGE_REPORTING,
            LLMCapability.STREAMING_USAGE_REPORTING,
            LLMCapability.CONTEXT_WINDOW,
            LLMCapability.MAX_OUTPUT_TOKENS,
        }
    ),
    tool_choice_modes=frozenset({"auto", "required"}),
    structured_output_strategies=frozenset({"native_schema"}),
    max_retry_attempts=2,
)

_DIALECTS_BY_ID = {
    policy.policy_id: policy
    for policy in (
        CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT,
        AGENT_OPENAI_COMPATIBLE_DIALECT,
    )
}


def resolve_openai_compatible_dialect(policy_id: str) -> LLMDialectPolicy:
    """Return a registered compatible dialect or fail closed."""

    normalized = str(policy_id).strip().lower()
    try:
        return _DIALECTS_BY_ID[normalized]
    except KeyError as exc:
        raise LLMConfigurationError(
            f"OpenAI-compatible dialect policy is not registered: {policy_id}",
            provider="OpenAI-compatible",
        ) from exc


def validate_openai_compatible_dialect(policy: LLMDialectPolicy) -> None:
    """Verify that policy data cannot expand the executable adapter ceiling."""

    policy.validate_adapter_binding(
        expected_adapter_id=OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
        allowed_capabilities=_ADAPTER_CAPABILITY_CEILING,
        allowed_tool_choice_modes=_ADAPTER_TOOL_CHOICE_MODES,
        allowed_structured_output_strategies=_ADAPTER_STRUCTURED_OUTPUT_STRATEGIES,
        allowed_reasoning_efforts=(),
        max_retry_attempts=2,
    )


__all__ = [
    "AGENT_OPENAI_COMPATIBLE_DIALECT",
    "CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT",
    "OPENAI_COMPATIBLE_CHAT_ADAPTER_ID",
    "OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION",
    "resolve_openai_compatible_dialect",
    "validate_openai_compatible_dialect",
]
