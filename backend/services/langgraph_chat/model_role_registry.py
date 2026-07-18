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
    InternalRoleModelBinding,
    ModelRoleRegistry,
    POST_TOOL_ARTICULATOR_MODEL_REF_ENV,
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
    TOOL_CATEGORY_SELECTOR_MODEL_REF_ENV,
    TOOL_OUTPUT_COMPRESSOR_MODEL_REF_ENV,
    validate_reasoning_effort_for_model,
)

__all__ = [
    "CANONICAL_REASONING_EFFORT_VALUES",
    "DEFAULT_CONVERSATION_MAIN_MODEL",
    "DEFAULT_INTERNAL_REASONING_EFFORT",
    "DEFAULT_USER_SELECTED_REASONING_EFFORT",
    "InternalRoleModelBinding",
    "ModelRoleRegistry",
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
    "RoleCallSettings",
    "RoleKey",
    "ReasoningEffort",
    "TOOL_CATEGORY_SELECTOR_MODEL_REF_ENV",
    "TOOL_OUTPUT_COMPRESSOR_MODEL_REF_ENV",
    "validate_reasoning_effort_for_model",
]
