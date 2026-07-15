"""
Shared LLM role contract types and constants.

This module defines the vocabulary shared by backend services and agent graph
code when they talk about role-owned LLM calls. It owns role keys, call source
labels, reasoning-effort literals, resolved call dataclasses, and the
provider/model override environment variable names.

Boundary: this file is contract-only. It must not import provider registries,
construct clients, read credentials, or encode provider-specific model
selection policy. Resolver behavior belongs in `role_policy.py`; provider-owned
internal defaults belong in provider profile builders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

ROLE_CONVERSATION_MAIN = "conversation_main"
ROLE_REASONING_MAIN = "reasoning_main"
ROLE_POST_TOOL_OBSERVATION = "post_tool_observation"
ROLE_INTENT_CLASSIFIER = "intent_classifier"
ROLE_CONTEXT_COMPRESSOR = "context_compressor"
ROLE_TOOL_OUTPUT_COMPRESSOR = "tool_output_compressor"
ROLE_TOOL_CATEGORY_SELECTOR = "tool_category_selector"
ROLE_POST_TOOL_ARTICULATOR = "post_tool_articulator"

CallSource = Literal["user_selected", "internal_fixed"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
RoleKey = Literal[
    "conversation_main",
    "reasoning_main",
    "post_tool_observation",
    "post_tool_articulator",
    "intent_classifier",
    "tool_output_compressor",
    "tool_category_selector",
]

DEFAULT_CONVERSATION_MAIN_MODEL = "gpt-5.2"
DEFAULT_USER_SELECTED_REASONING_EFFORT: ReasoningEffort = "medium"
DEFAULT_INTERNAL_REASONING_EFFORT: ReasoningEffort = "minimal"
CANONICAL_REASONING_EFFORT_VALUES: tuple[ReasoningEffort, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
DEFAULT_PROVIDER_ID = "openai"

TOOL_OUTPUT_COMPRESSOR_MODEL_REF_ENV = "LANGGRAPH_TOOL_OUTPUT_COMPRESSOR_MODEL_REF"
TOOL_CATEGORY_SELECTOR_MODEL_REF_ENV = "LANGGRAPH_TOOL_CATEGORY_SELECTOR_MODEL_REF"
POST_TOOL_ARTICULATOR_MODEL_REF_ENV = "LANGGRAPH_POST_TOOL_ARTICULATOR_MODEL_REF"


@dataclass(frozen=True, slots=True)
class RoleCallSettings:
    """Resolved call settings for one role-owned LLM invocation."""

    provider: str
    model: str
    reasoning_effort: Optional[ReasoningEffort]
    source: CallSource


@dataclass(frozen=True, slots=True)
class ProviderModelBinding:
    """Provider/model pair resolved by role policy without importing adapters."""

    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class InternalRoleModelBinding:
    """Provider-scoped default model for an internal role."""

    role: RoleKey
    provider: str
    model: str


def internal_role_model_ref_env(role: RoleKey) -> str:
    """Return the provider/model override env var for an internal role."""
    if role == ROLE_TOOL_OUTPUT_COMPRESSOR:
        return TOOL_OUTPUT_COMPRESSOR_MODEL_REF_ENV
    if role == ROLE_TOOL_CATEGORY_SELECTOR:
        return TOOL_CATEGORY_SELECTOR_MODEL_REF_ENV
    if role == ROLE_POST_TOOL_ARTICULATOR:
        return POST_TOOL_ARTICULATOR_MODEL_REF_ENV
    raise ValueError(f"Unknown internal model role: {role}")


__all__ = [
    "CANONICAL_REASONING_EFFORT_VALUES",
    "CallSource",
    "DEFAULT_CONVERSATION_MAIN_MODEL",
    "DEFAULT_INTERNAL_REASONING_EFFORT",
    "DEFAULT_PROVIDER_ID",
    "DEFAULT_USER_SELECTED_REASONING_EFFORT",
    "InternalRoleModelBinding",
    "POST_TOOL_ARTICULATOR_MODEL_REF_ENV",
    "ProviderModelBinding",
    "ROLE_CONTEXT_COMPRESSOR",
    "ROLE_CONVERSATION_MAIN",
    "ROLE_INTENT_CLASSIFIER",
    "ROLE_POST_TOOL_ARTICULATOR",
    "ROLE_POST_TOOL_OBSERVATION",
    "ROLE_REASONING_MAIN",
    "ROLE_TOOL_CATEGORY_SELECTOR",
    "ROLE_TOOL_OUTPUT_COMPRESSOR",
    "ReasoningEffort",
    "RoleCallSettings",
    "RoleKey",
    "TOOL_CATEGORY_SELECTOR_MODEL_REF_ENV",
    "TOOL_OUTPUT_COMPRESSOR_MODEL_REF_ENV",
    "internal_role_model_ref_env",
]
