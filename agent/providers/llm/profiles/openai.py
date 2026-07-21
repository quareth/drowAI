"""OpenAI provider and model profile data builders."""

from __future__ import annotations

from typing import Any

from ..contracts.structured_output_strategy import freeze_structured_output_strategies
from ..contracts.tool_contracts import freeze_tool_choice_modes
from ..core.capabilities import LLMCapability, freeze_capabilities
from ..core.identity import (
    OPENAI_GPT35_FAMILY,
    OPENAI_GPT4_FAMILY,
    OPENAI_GPT5_FAMILY,
    OPENAI_PROVIDER_ID,
    ProviderModelRef,
)
from .registry import (
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    ModelProfile,
    ProviderProfile,
    _CompatibilityRule,
)

OPENAI_DEFAULT_MODEL_ID = "gpt-5.2"

OPENAI_API_SURFACE_RESPONSES = "responses"
OPENAI_API_SURFACE_CHAT_COMPLETIONS = "chat_completions"
OPENAI_RESPONSES_MAX_OUTPUT_TOKENS = 32_000
OPENAI_GPT_OSS_20B_MODEL_ID = "gpt-oss-20b"

OPENAI_LISTABLE_MODEL_IDS: tuple[str, ...] = (
    "gpt-5",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5-pro",
    "gpt-5.1",
    "gpt-5.2",
    "gpt-5.2-pro",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.5",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    OPENAI_GPT_OSS_20B_MODEL_ID,
)

OPENAI_NON_LISTABLE_RESPONSES_MODEL_IDS: tuple[str, ...] = (
    "gpt-5.4-pro",
    "gpt-5.5-pro",
    "gpt-5.6",
)

OPENAI_LEGACY_CHAT_MODEL_IDS: tuple[str, ...] = (
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4-turbo",
    "gpt-4",
    "gpt-3.5-turbo",
    "gpt-3.5",
)

OPENAI_EXACT_MODEL_IDS: tuple[str, ...] = (
    *OPENAI_LISTABLE_MODEL_IDS,
    *OPENAI_NON_LISTABLE_RESPONSES_MODEL_IDS,
    *OPENAI_LEGACY_CHAT_MODEL_IDS,
)

OPENAI_LISTABLE_MODEL_LABELS: dict[str, str] = {
    "gpt-5": "GPT-5 (Standard)",
    "gpt-5-mini": "GPT-5 Mini (Fast)",
    "gpt-5-nano": "GPT-5 Nano (Lightweight)",
    "gpt-5-pro": "GPT-5 Pro (Advanced)",
    "gpt-5.1": "GPT-5.1",
    "gpt-5.2": "GPT-5.2",
    "gpt-5.2-pro": "GPT-5.2 Pro",
    "gpt-5.4": "GPT-5.4",
    "gpt-5.4-mini": "GPT-5.4 Mini",
    "gpt-5.4-nano": "GPT-5.4 Nano",
    "gpt-5.4-pro": "GPT-5.4 Pro",
    "gpt-5.5": "GPT-5.5",
    "gpt-5.5-pro": "GPT-5.5 Pro",
    "gpt-5.6": "GPT-5.6 Sol (Alias)",
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gpt-5.6-terra": "GPT-5.6 Terra",
    "gpt-5.6-luna": "GPT-5.6 Luna",
    OPENAI_GPT_OSS_20B_MODEL_ID: "GPT-OSS 20B",
}

_OPENAI_MODEL_CAPABILITIES = freeze_capabilities(
    (
        LLMCapability.CHAT,
        LLMCapability.STREAMING,
        LLMCapability.TOOLS,
        LLMCapability.PARALLEL_TOOLS,
        LLMCapability.STRUCTURED_OUTPUT_NATIVE,
        LLMCapability.USAGE_REPORTING,
        LLMCapability.STREAMING_USAGE_REPORTING,
        LLMCapability.CONTEXT_WINDOW,
        LLMCapability.MAX_OUTPUT_TOKENS,
    )
)

_OPENAI_RESPONSES_MODEL_CAPABILITIES = _OPENAI_MODEL_CAPABILITIES | frozenset(
    {LLMCapability.REASONING_EFFORT}
)
_OPENAI_RESPONSES_NON_STREAMING_MODEL_CAPABILITIES = (
    _OPENAI_RESPONSES_MODEL_CAPABILITIES
    - frozenset(
        {
            LLMCapability.STREAMING,
            LLMCapability.STREAMING_USAGE_REPORTING,
        }
    )
)
_OPENAI_RESPONSES_NO_NATIVE_STRUCTURED_OUTPUT_CAPABILITIES = (
    _OPENAI_RESPONSES_MODEL_CAPABILITIES
    - frozenset({LLMCapability.STRUCTURED_OUTPUT_NATIVE})
)
_OPENAI_COMPATIBLE_CHAT_AGENT_CAPABILITIES = freeze_capabilities(
    (
        LLMCapability.CHAT,
        LLMCapability.STREAMING,
        LLMCapability.TOOLS,
        LLMCapability.STRUCTURED_OUTPUT_NATIVE,
        LLMCapability.USAGE_REPORTING,
        LLMCapability.STREAMING_USAGE_REPORTING,
        LLMCapability.CONTEXT_WINDOW,
        LLMCapability.MAX_OUTPUT_TOKENS,
    )
)

_OPENAI_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high"})
_OPENAI_PRO_REASONING_EFFORTS = _OPENAI_REASONING_EFFORTS | frozenset({"xhigh"})
_OPENAI_XHIGH_REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh"})
_OPENAI_PRO_XHIGH_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})
_OPENAI_GPT56_REASONING_EFFORTS = frozenset(
    {"none", "low", "medium", "high", "xhigh", "max"}
)
_OPENAI_TOOL_CHOICE_MODES = freeze_tool_choice_modes(
    ("auto", "none", "required", "specific")
)
_OPENAI_STRUCTURED_OUTPUT_STRATEGIES = freeze_structured_output_strategies(
    ("native_schema",)
)
_OPENAI_NO_STRUCTURED_OUTPUT_STRATEGIES = freeze_structured_output_strategies(())

_OPENAI_RESPONSES_MODEL_OVERRIDES: dict[str, dict[str, Any]] = {
    "gpt-5.4": {
        "context_window_tokens": 1_050_000,
        "max_output_tokens": 128_000,
        "reasoning_efforts": _OPENAI_XHIGH_REASONING_EFFORTS,
        "default_reasoning_effort": "medium",
    },
    "gpt-5.4-mini": {
        "context_window_tokens": 400_000,
        "max_output_tokens": 128_000,
        "reasoning_efforts": _OPENAI_XHIGH_REASONING_EFFORTS,
        "default_reasoning_effort": "medium",
    },
    "gpt-5.4-nano": {
        "context_window_tokens": 400_000,
        "max_output_tokens": 128_000,
        "reasoning_efforts": _OPENAI_XHIGH_REASONING_EFFORTS,
        "default_reasoning_effort": "medium",
    },
    "gpt-5.4-pro": {
        "capabilities": _OPENAI_RESPONSES_NO_NATIVE_STRUCTURED_OUTPUT_CAPABILITIES,
        "context_window_tokens": 1_050_000,
        "max_output_tokens": 128_000,
        "reasoning_efforts": _OPENAI_PRO_XHIGH_REASONING_EFFORTS,
        "default_reasoning_effort": "medium",
        "structured_output_strategies": _OPENAI_NO_STRUCTURED_OUTPUT_STRATEGIES,
    },
    "gpt-5.5": {
        "context_window_tokens": 1_050_000,
        "max_output_tokens": 128_000,
        "reasoning_efforts": _OPENAI_XHIGH_REASONING_EFFORTS,
        "default_reasoning_effort": "medium",
    },
    "gpt-5.5-pro": {
        "capabilities": _OPENAI_RESPONSES_NON_STREAMING_MODEL_CAPABILITIES,
        "context_window_tokens": 1_050_000,
        "max_output_tokens": 128_000,
        "reasoning_efforts": _OPENAI_PRO_XHIGH_REASONING_EFFORTS,
        "default_reasoning_effort": "high",
    },
    "gpt-5.6": {
        "context_window_tokens": 1_050_000,
        "max_output_tokens": 128_000,
        "reasoning_efforts": _OPENAI_GPT56_REASONING_EFFORTS,
        "default_reasoning_effort": "medium",
    },
    "gpt-5.6-sol": {
        "context_window_tokens": 1_050_000,
        "max_output_tokens": 128_000,
        "reasoning_efforts": _OPENAI_GPT56_REASONING_EFFORTS,
        "default_reasoning_effort": "medium",
    },
    "gpt-5.6-terra": {
        "context_window_tokens": 1_050_000,
        "max_output_tokens": 128_000,
        "reasoning_efforts": _OPENAI_GPT56_REASONING_EFFORTS,
        "default_reasoning_effort": "medium",
    },
    "gpt-5.6-luna": {
        "context_window_tokens": 1_050_000,
        "max_output_tokens": 128_000,
        "reasoning_efforts": _OPENAI_GPT56_REASONING_EFFORTS,
        "default_reasoning_effort": "medium",
    },
}


def build_openai_provider_profile() -> ProviderProfile:
    """Build the OpenAI provider-wide profile."""
    return ProviderProfile(
        id=OPENAI_PROVIDER_ID,
        display_name="OpenAI",
        capabilities=freeze_capabilities((LLMCapability.REMOTE_CONVERSATION_LIFECYCLE,)),
    )


def build_openai_model_profiles() -> tuple[ModelProfile, ...]:
    """Build exact OpenAI model profiles in registry insertion order."""
    profiles: list[ModelProfile] = [
        (
            _openai_compatible_chat_profile(model_id, listable=True)
            if model_id == OPENAI_GPT_OSS_20B_MODEL_ID
            else _openai_responses_profile(model_id, listable=True)
        )
        for model_id in OPENAI_LISTABLE_MODEL_IDS
    ]
    profiles.extend(
        _openai_responses_profile(model_id, listable=False)
        for model_id in OPENAI_NON_LISTABLE_RESPONSES_MODEL_IDS
    )
    profiles.extend(
        _openai_chat_profile(model_id)
        for model_id in OPENAI_LEGACY_CHAT_MODEL_IDS
    )
    return tuple(profiles)


def build_openai_compatibility_rules() -> tuple[_CompatibilityRule, ...]:
    """Build approved OpenAI legacy family compatibility rules."""
    return (
        _CompatibilityRule(
            provider=OPENAI_PROVIDER_ID,
            family_prefix=OPENAI_GPT35_FAMILY,
            template=_openai_compatibility_template(
                OPENAI_GPT35_FAMILY,
                api_surface=OPENAI_API_SURFACE_CHAT_COMPLETIONS,
            ),
        ),
        _CompatibilityRule(
            provider=OPENAI_PROVIDER_ID,
            family_prefix=OPENAI_GPT5_FAMILY,
            template=_openai_compatibility_template(
                OPENAI_GPT5_FAMILY,
                api_surface=OPENAI_API_SURFACE_RESPONSES,
            ),
        ),
        _CompatibilityRule(
            provider=OPENAI_PROVIDER_ID,
            family_prefix=OPENAI_GPT4_FAMILY,
            template=_openai_compatibility_template(
                OPENAI_GPT4_FAMILY,
                api_surface=OPENAI_API_SURFACE_CHAT_COMPLETIONS,
            ),
        ),
    )


def _openai_responses_profile(model_id: str, *, listable: bool) -> ModelProfile:
    """Build an exact OpenAI Responses profile with conservative tenant_baseline limits."""
    overrides = _OPENAI_RESPONSES_MODEL_OVERRIDES.get(model_id, {})
    reasoning_efforts = overrides.get(
        "reasoning_efforts",
        (
            _OPENAI_PRO_REASONING_EFFORTS
            if model_id == "gpt-5.2-pro"
            else _OPENAI_REASONING_EFFORTS
        ),
    )
    capabilities = overrides.get("capabilities", _OPENAI_RESPONSES_MODEL_CAPABILITIES)
    structured_output_strategies = overrides.get(
        "structured_output_strategies",
        _OPENAI_STRUCTURED_OUTPUT_STRATEGIES,
    )
    return ModelProfile(
        ref=ProviderModelRef(OPENAI_PROVIDER_ID, model_id),
        display_name=OPENAI_LISTABLE_MODEL_LABELS.get(model_id, model_id),
        api_surface=OPENAI_API_SURFACE_RESPONSES,
        capabilities=capabilities,
        context_window_tokens=int(
            overrides.get("context_window_tokens", DEFAULT_CONTEXT_WINDOW_TOKENS)
        ),
        max_output_tokens=int(
            overrides.get("max_output_tokens", OPENAI_RESPONSES_MAX_OUTPUT_TOKENS)
        ),
        listable=listable,
        reasoning_efforts=reasoning_efforts,
        default_reasoning_effort=str(
            overrides.get("default_reasoning_effort", "minimal")
        ),
        tool_choice_modes=_OPENAI_TOOL_CHOICE_MODES,
        structured_output_strategies=structured_output_strategies,
    )


def _openai_chat_profile(model_id: str) -> ModelProfile:
    """Build an exact OpenAI Chat Completions profile for legacy routing."""
    return ModelProfile(
        ref=ProviderModelRef(OPENAI_PROVIDER_ID, model_id),
        display_name=model_id,
        api_surface=OPENAI_API_SURFACE_CHAT_COMPLETIONS,
        capabilities=_OPENAI_MODEL_CAPABILITIES,
        context_window_tokens=DEFAULT_CONTEXT_WINDOW_TOKENS,
        max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        listable=False,
        tool_choice_modes=_OPENAI_TOOL_CHOICE_MODES,
        structured_output_strategies=_OPENAI_STRUCTURED_OUTPUT_STRATEGIES,
    )


def _openai_compatible_chat_profile(model_id: str, *, listable: bool) -> ModelProfile:
    """Build the reviewed agent profile for GPT-OSS compatible deployments."""
    return ModelProfile(
        ref=ProviderModelRef(OPENAI_PROVIDER_ID, model_id),
        display_name=OPENAI_LISTABLE_MODEL_LABELS.get(model_id, model_id),
        api_surface=OPENAI_API_SURFACE_CHAT_COMPLETIONS,
        capabilities=_OPENAI_COMPATIBLE_CHAT_AGENT_CAPABILITIES,
        context_window_tokens=DEFAULT_CONTEXT_WINDOW_TOKENS,
        max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
        listable=listable,
        tool_choice_modes=frozenset({"auto", "required"}),
        structured_output_strategies=_OPENAI_STRUCTURED_OUTPUT_STRATEGIES,
    )


def _openai_compatibility_template(family: str, *, api_surface: str) -> ModelProfile:
    """Build a non-catalog template for approved OpenAI family fallbacks."""
    capabilities = (
        _OPENAI_RESPONSES_MODEL_CAPABILITIES
        if api_surface == OPENAI_API_SURFACE_RESPONSES
        else _OPENAI_MODEL_CAPABILITIES
    )
    reasoning_efforts = _OPENAI_REASONING_EFFORTS if family == OPENAI_GPT5_FAMILY else frozenset()
    default_reasoning_effort = "minimal" if family == OPENAI_GPT5_FAMILY else None
    max_output_tokens = (
        OPENAI_RESPONSES_MAX_OUTPUT_TOKENS
        if api_surface == OPENAI_API_SURFACE_RESPONSES
        else DEFAULT_MAX_OUTPUT_TOKENS
    )
    return ModelProfile(
        ref=ProviderModelRef(OPENAI_PROVIDER_ID, f"{family}-compatibility"),
        display_name=f"OpenAI {family} compatibility profile",
        api_surface=api_surface,
        capabilities=capabilities,
        context_window_tokens=DEFAULT_CONTEXT_WINDOW_TOKENS,
        max_output_tokens=max_output_tokens,
        listable=False,
        compatibility_family=family,
        reasoning_efforts=reasoning_efforts,
        default_reasoning_effort=default_reasoning_effort,
        tool_choice_modes=_OPENAI_TOOL_CHOICE_MODES,
        structured_output_strategies=_OPENAI_STRUCTURED_OUTPUT_STRATEGIES,
    )


__all__ = [
    "OPENAI_API_SURFACE_CHAT_COMPLETIONS",
    "OPENAI_API_SURFACE_RESPONSES",
    "OPENAI_DEFAULT_MODEL_ID",
    "OPENAI_EXACT_MODEL_IDS",
    "OPENAI_GPT_OSS_20B_MODEL_ID",
    "OPENAI_LEGACY_CHAT_MODEL_IDS",
    "OPENAI_LISTABLE_MODEL_IDS",
    "OPENAI_NON_LISTABLE_RESPONSES_MODEL_IDS",
    "OPENAI_RESPONSES_MAX_OUTPUT_TOKENS",
    "build_openai_compatibility_rules",
    "build_openai_model_profiles",
    "build_openai_provider_profile",
]
