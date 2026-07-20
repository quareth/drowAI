"""
Shared LLM role contract types and constants.

This module defines the vocabulary shared by backend services and agent graph
code when they talk about role-owned LLM calls. It owns role keys, call source
labels, reasoning-effort literals, and resolved call dataclasses.

Boundary: this file is contract-only. It must not import provider registries,
construct clients, read credentials, or encode provider-specific model
selection policy. Resolver behavior belongs in `role_policy.py`; concrete model
capabilities belong in provider profile builders.
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

CallSource = Literal["user_selected"]
ReasoningEffort = Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"]
RoleKey = Literal[
    "conversation_main",
    "reasoning_main",
    "post_tool_observation",
    "post_tool_articulator",
    "intent_classifier",
    "context_compressor",
    "tool_output_compressor",
    "tool_category_selector",
]

DEFAULT_CONVERSATION_MAIN_MODEL = "gpt-5.2"
DEFAULT_USER_SELECTED_REASONING_EFFORT: ReasoningEffort = "medium"
DEFAULT_INTERNAL_REASONING_EFFORT: ReasoningEffort = "low"
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


__all__ = [
    "CANONICAL_REASONING_EFFORT_VALUES",
    "CallSource",
    "DEFAULT_CONVERSATION_MAIN_MODEL",
    "DEFAULT_INTERNAL_REASONING_EFFORT",
    "DEFAULT_PROVIDER_ID",
    "DEFAULT_USER_SELECTED_REASONING_EFFORT",
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
]
