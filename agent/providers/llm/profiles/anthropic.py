"""Anthropic provider and model profile data builders."""

from __future__ import annotations

from core.llm.role_contracts import (
    ROLE_POST_TOOL_ARTICULATOR,
    ROLE_TOOL_CATEGORY_SELECTOR,
    ROLE_TOOL_OUTPUT_COMPRESSOR,
)

from ..contracts.structured_output_strategy import freeze_structured_output_strategies
from ..contracts.tool_contracts import freeze_tool_choice_modes
from ..core.capabilities import LLMCapability, freeze_capabilities
from ..core.identity import ANTHROPIC_PROVIDER_ID, ProviderModelRef
from .registry import ModelProfile, ProviderProfile

ANTHROPIC_API_SURFACE_MESSAGES = "messages"
ANTHROPIC_DEFAULT_MODEL_ID = "claude-sonnet-4-6"

ANTHROPIC_LISTABLE_MODEL_IDS: tuple[str, ...] = (
    "claude-fable-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
)

ANTHROPIC_NON_LISTABLE_MODEL_IDS: tuple[str, ...] = (
    "claude-mythos-5",
)

ANTHROPIC_EXACT_MODEL_IDS: tuple[str, ...] = (
    *ANTHROPIC_LISTABLE_MODEL_IDS,
    *ANTHROPIC_NON_LISTABLE_MODEL_IDS,
)

ANTHROPIC_MODEL_LABELS: dict[str, str] = {
    "claude-fable-5": "Claude Fable 5",
    "claude-mythos-5": "Claude Mythos 5",
    "claude-opus-4-7": "Claude Opus 4.7",
    "claude-opus-4-8": "Claude Opus 4.8",
    "claude-sonnet-5": "Claude Sonnet 5",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5-20251001": "Claude Haiku 4.5",
}

ANTHROPIC_MODEL_LIMITS: dict[str, tuple[int, int]] = {
    "claude-fable-5": (1_000_000, 128_000),
    "claude-mythos-5": (1_000_000, 128_000),
    "claude-opus-4-7": (1_000_000, 128_000),
    "claude-opus-4-8": (1_000_000, 128_000),
    "claude-sonnet-5": (1_000_000, 128_000),
    "claude-sonnet-4-6": (1_000_000, 64_000),
    "claude-haiku-4-5-20251001": (200_000, 64_000),
}

ANTHROPIC_INTERNAL_ROLE_MODELS: dict[str, str] = {
    ROLE_TOOL_OUTPUT_COMPRESSOR: "claude-haiku-4-5-20251001",
    ROLE_TOOL_CATEGORY_SELECTOR: "claude-haiku-4-5-20251001",
    ROLE_POST_TOOL_ARTICULATOR: "claude-haiku-4-5-20251001",
}

_ANTHROPIC_MESSAGES_MODEL_CAPABILITIES = freeze_capabilities(
    (
        LLMCapability.CHAT,
        LLMCapability.STREAMING,
        LLMCapability.TOOLS,
        LLMCapability.USAGE_REPORTING,
        LLMCapability.STREAMING_USAGE_REPORTING,
        LLMCapability.CONTEXT_WINDOW,
        LLMCapability.MAX_OUTPUT_TOKENS,
    )
)
_ANTHROPIC_REASONING_MODEL_CAPABILITIES = (
    _ANTHROPIC_MESSAGES_MODEL_CAPABILITIES
    | frozenset({LLMCapability.REASONING_EFFORT})
)
_ANTHROPIC_STANDARD_REASONING_EFFORTS = frozenset(
    {"low", "medium", "high", "max"}
)
_ANTHROPIC_XHIGH_REASONING_EFFORTS = (
    _ANTHROPIC_STANDARD_REASONING_EFFORTS | frozenset({"xhigh"})
)
_ANTHROPIC_REASONING_EFFORTS_BY_MODEL: dict[str, frozenset[str]] = {
    "claude-fable-5": _ANTHROPIC_XHIGH_REASONING_EFFORTS,
    "claude-mythos-5": _ANTHROPIC_XHIGH_REASONING_EFFORTS,
    "claude-opus-4-7": _ANTHROPIC_XHIGH_REASONING_EFFORTS,
    "claude-opus-4-8": _ANTHROPIC_XHIGH_REASONING_EFFORTS,
    "claude-sonnet-5": _ANTHROPIC_XHIGH_REASONING_EFFORTS,
    "claude-sonnet-4-6": _ANTHROPIC_STANDARD_REASONING_EFFORTS,
}
_ANTHROPIC_TOOL_CHOICE_MODES = freeze_tool_choice_modes(
    ("auto", "none", "required", "specific")
)
_ANTHROPIC_STRUCTURED_OUTPUT_STRATEGIES = freeze_structured_output_strategies(
    ("prompt_parse",)
)


def build_anthropic_provider_profile() -> ProviderProfile:
    """Build the Anthropic provider-wide profile."""
    return ProviderProfile(
        id=ANTHROPIC_PROVIDER_ID,
        display_name="Anthropic",
        capabilities=frozenset(),
        internal_role_models=ANTHROPIC_INTERNAL_ROLE_MODELS,
    )


def build_anthropic_model_profiles() -> tuple[ModelProfile, ...]:
    """Build exact Anthropic model profiles in registry insertion order."""
    profiles = [
        _anthropic_messages_profile(model_id, listable=True)
        for model_id in ANTHROPIC_LISTABLE_MODEL_IDS
    ]
    profiles.extend(
        _anthropic_messages_profile(model_id, listable=False)
        for model_id in ANTHROPIC_NON_LISTABLE_MODEL_IDS
    )
    return tuple(profiles)


def _anthropic_messages_profile(model_id: str, *, listable: bool) -> ModelProfile:
    """Build an exact Anthropic Messages profile with provider-published limits."""
    context_window_tokens, max_output_tokens = ANTHROPIC_MODEL_LIMITS[model_id]
    reasoning_efforts = _ANTHROPIC_REASONING_EFFORTS_BY_MODEL.get(
        model_id,
        frozenset(),
    )
    return ModelProfile(
        ref=ProviderModelRef(ANTHROPIC_PROVIDER_ID, model_id),
        display_name=ANTHROPIC_MODEL_LABELS.get(model_id, model_id),
        api_surface=ANTHROPIC_API_SURFACE_MESSAGES,
        capabilities=(
            _ANTHROPIC_REASONING_MODEL_CAPABILITIES
            if reasoning_efforts
            else _ANTHROPIC_MESSAGES_MODEL_CAPABILITIES
        ),
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
        listable=listable,
        reasoning_efforts=reasoning_efforts,
        default_reasoning_effort="high" if reasoning_efforts else None,
        tool_choice_modes=_ANTHROPIC_TOOL_CHOICE_MODES,
        structured_output_strategies=_ANTHROPIC_STRUCTURED_OUTPUT_STRATEGIES,
    )


__all__ = [
    "ANTHROPIC_API_SURFACE_MESSAGES",
    "ANTHROPIC_DEFAULT_MODEL_ID",
    "ANTHROPIC_EXACT_MODEL_IDS",
    "ANTHROPIC_INTERNAL_ROLE_MODELS",
    "ANTHROPIC_LISTABLE_MODEL_IDS",
    "ANTHROPIC_NON_LISTABLE_MODEL_IDS",
    "build_anthropic_model_profiles",
    "build_anthropic_provider_profile",
]
