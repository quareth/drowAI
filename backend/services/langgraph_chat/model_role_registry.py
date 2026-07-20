"""
Compatibility shim for role-based model/effort policy.

This module intentionally re-exports the canonical shared policy authority
from `core.llm.role_policy` to preserve existing import paths while keeping
one implementation source of truth.
"""

from core.llm.role_policy import (
    CANONICAL_REASONING_EFFORT_VALUES,
    DEFAULT_CONVERSATION_MAIN_MODEL,
    DEFAULT_INTERNAL_REASONING_EFFORT,
    DEFAULT_USER_SELECTED_REASONING_EFFORT,
    ModelRoleRegistry,
    ProviderModelBinding,
    ROLE_CONTEXT_COMPRESSOR,
    ROLE_CONVERSATION_MAIN,
    ROLE_INTENT_CLASSIFIER,
    ROLE_POST_TOOL_ARTICULATOR,
    ROLE_POST_TOOL_OBSERVATION,
    ROLE_REASONING_MAIN,
    ROLE_TOOL_CATEGORY_SELECTOR,
    ROLE_TOOL_OUTPUT_COMPRESSOR,
    RoleCallSettings,
    RoleKey,
    ReasoningEffort,
    validate_reasoning_effort_for_model,
)

__all__ = [
    "CANONICAL_REASONING_EFFORT_VALUES",
    "DEFAULT_CONVERSATION_MAIN_MODEL",
    "DEFAULT_INTERNAL_REASONING_EFFORT",
    "DEFAULT_USER_SELECTED_REASONING_EFFORT",
    "ModelRoleRegistry",
    "ProviderModelBinding",
    "ROLE_CONTEXT_COMPRESSOR",
    "ROLE_CONVERSATION_MAIN",
    "ROLE_INTENT_CLASSIFIER",
    "ROLE_POST_TOOL_ARTICULATOR",
    "ROLE_POST_TOOL_OBSERVATION",
    "ROLE_REASONING_MAIN",
    "ROLE_TOOL_CATEGORY_SELECTOR",
    "ROLE_TOOL_OUTPUT_COMPRESSOR",
    "RoleCallSettings",
    "RoleKey",
    "ReasoningEffort",
    "validate_reasoning_effort_for_model",
]
