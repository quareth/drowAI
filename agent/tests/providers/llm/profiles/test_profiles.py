"""Tests for provider/model profile metadata and capability checks."""

from __future__ import annotations

import pytest

from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.exceptions import LLMCapabilityNotSupportedError, LLMProfileNotFoundError
from agent.providers.llm.core.identity import ANTHROPIC_PROVIDER_ID, OPENAI_PROVIDER_ID, ProviderModelRef
from agent.providers.llm.profiles.registry import (
    ANTHROPIC_EXACT_MODEL_IDS,
    ANTHROPIC_LISTABLE_MODEL_IDS,
    ANTHROPIC_NON_LISTABLE_MODEL_IDS,
    OPENAI_API_SURFACE_CHAT_COMPLETIONS,
    OPENAI_API_SURFACE_RESPONSES,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    OPENAI_INTERNAL_ROLE_MODELS,
    OPENAI_EXACT_MODEL_IDS,
    OPENAI_LEGACY_CHAT_MODEL_IDS,
    OPENAI_LISTABLE_MODEL_IDS,
    OPENAI_NON_LISTABLE_RESPONSES_MODEL_IDS,
    OPENAI_RESPONSES_MAX_OUTPUT_TOKENS,
    get_default_model_ref,
    list_catalog_model_profiles,
    list_model_profiles,
    require_model_capability,
    require_model_profile,
    require_provider_capability,
    require_provider_profile,
    resolve_provider_internal_role_model,
    resolve_context_window_tokens,
    resolve_max_output_tokens,
    supports_model,
    supports_provider,
)
from core.llm.role_contracts import (
    ROLE_POST_TOOL_ARTICULATOR,
    ROLE_TOOL_CATEGORY_SELECTOR,
    ROLE_TOOL_OUTPUT_COMPRESSOR,
)


def _openai_ref(model: str) -> ProviderModelRef:
    return ProviderModelRef(OPENAI_PROVIDER_ID, model)


def test_default_model_ref_is_current_openai_default() -> None:
    assert get_default_model_ref() == ProviderModelRef(OPENAI_PROVIDER_ID, "gpt-5.2")


def test_openai_provider_profile_declares_provider_wide_capability_only() -> None:
    profile = require_provider_profile("OpenAI")

    assert profile.id == OPENAI_PROVIDER_ID
    assert profile.display_name == "OpenAI"
    assert supports_provider(OPENAI_PROVIDER_ID, LLMCapability.REMOTE_CONVERSATION_LIFECYCLE)
    assert not supports_provider(OPENAI_PROVIDER_ID, LLMCapability.REASONING_EFFORT)
    assert dict(profile.internal_role_models) == OPENAI_INTERNAL_ROLE_MODELS


def test_provider_capability_checks_fail_loudly() -> None:
    with pytest.raises(LLMCapabilityNotSupportedError, match="does not support capability"):
        require_provider_capability(OPENAI_PROVIDER_ID, LLMCapability.REASONING_EFFORT)


def test_every_exact_openai_model_has_profile_and_limits() -> None:
    for model_id in OPENAI_EXACT_MODEL_IDS:
        profile = require_model_profile(_openai_ref(model_id))

        assert profile.ref == _openai_ref(model_id)
        assert profile.compatibility_family is None
        assert profile.context_window_tokens > 0
        if profile.api_surface == OPENAI_API_SURFACE_RESPONSES:
            assert profile.max_output_tokens >= OPENAI_RESPONSES_MAX_OUTPUT_TOKENS
        elif profile.api_surface == OPENAI_API_SURFACE_CHAT_COMPLETIONS:
            assert profile.context_window_tokens == DEFAULT_CONTEXT_WINDOW_TOKENS
            assert profile.max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS
        else:
            raise AssertionError(f"Unexpected OpenAI API surface: {profile.api_surface}")


def test_openai_profiles_include_runner_control_strategy_metadata() -> None:
    model_ids = (*OPENAI_EXACT_MODEL_IDS, "gpt-5-preview")

    for model_id in model_ids:
        profile = require_model_profile(_openai_ref(model_id))

        if model_id in {"gpt-5.5-pro", "gpt-oss-20b"}:
            assert not profile.supports(LLMCapability.STREAMING)
            assert not profile.supports(LLMCapability.STREAMING_USAGE_REPORTING)
        else:
            assert profile.supports(LLMCapability.STREAMING_USAGE_REPORTING)
        if model_id == "gpt-oss-20b":
            assert profile.tool_choice_modes == frozenset()
        else:
            assert profile.tool_choice_modes == frozenset(
                ("auto", "none", "required", "specific")
            )
        if model_id in {"gpt-5.4-pro", "gpt-oss-20b"}:
            assert not profile.supports(LLMCapability.STRUCTURED_OUTPUT_NATIVE)
            assert profile.structured_output_strategies == frozenset()
        else:
            assert profile.structured_output_strategies == frozenset(("native_schema",))


def test_catalog_profiles_are_curated_listable_openai_models_only() -> None:
    catalog_ids = tuple(profile.ref.model for profile in list_catalog_model_profiles())

    assert catalog_ids == tuple(sorted(OPENAI_LISTABLE_MODEL_IDS))
    assert "gpt-4o" not in catalog_ids
    assert "gpt-3.5-turbo" not in catalog_ids
    assert "gpt-5-preview" not in catalog_ids
    assert "gpt-5.4-pro" not in catalog_ids
    assert "gpt-5.5-pro" not in catalog_ids
    assert all(profile.listable is True for profile in list_catalog_model_profiles())


def test_new_openai_models_are_registered_with_expected_catalog_visibility() -> None:
    catalog_ids = {
        profile.ref.model for profile in list_catalog_model_profiles(OPENAI_PROVIDER_ID)
    }
    non_listable_ids = {
        profile.ref.model
        for profile in list_model_profiles(provider_id=OPENAI_PROVIDER_ID, listable=False)
    }

    assert {
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.5",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    }.issubset(catalog_ids)
    assert set(OPENAI_NON_LISTABLE_RESPONSES_MODEL_IDS).issubset(non_listable_ids)
    assert set(OPENAI_NON_LISTABLE_RESPONSES_MODEL_IDS).isdisjoint(catalog_ids)


def test_legacy_chat_exact_profiles_are_not_listable() -> None:
    non_listable = {
        profile.ref.model
        for profile in list_model_profiles(provider_id=OPENAI_PROVIDER_ID, listable=False)
    }

    assert set(OPENAI_LEGACY_CHAT_MODEL_IDS).issubset(non_listable)


def test_anthropic_profiles_are_registered_with_runner_control_policy_metadata() -> None:
    provider = require_provider_profile("Anthropic")

    assert provider.id == ANTHROPIC_PROVIDER_ID
    assert provider.display_name == "Anthropic"
    assert provider.capabilities == frozenset()
    assert set(provider.internal_role_models) == {
        ROLE_TOOL_OUTPUT_COMPRESSOR,
        ROLE_TOOL_CATEGORY_SELECTOR,
        ROLE_POST_TOOL_ARTICULATOR,
    }

    for model_id in ANTHROPIC_LISTABLE_MODEL_IDS:
        profile = require_model_profile(ProviderModelRef(ANTHROPIC_PROVIDER_ID, model_id))

        assert profile.ref.provider == ANTHROPIC_PROVIDER_ID
        assert profile.api_surface == "messages"
        assert profile.listable is True
        assert profile.supports(LLMCapability.CHAT)
        assert profile.supports(LLMCapability.STREAMING)
        assert profile.supports(LLMCapability.TOOLS)
        assert profile.supports(LLMCapability.USAGE_REPORTING)
        assert not profile.supports(LLMCapability.STRUCTURED_OUTPUT_NATIVE)
        assert not profile.supports(LLMCapability.STRUCTURED_OUTPUT_TOOL_FALLBACK)
        assert profile.supports(LLMCapability.STREAMING_USAGE_REPORTING)
        assert profile.tool_choice_modes == frozenset(
            ("auto", "none", "required", "specific")
        )
        assert profile.structured_output_strategies == frozenset(("prompt_parse",))


@pytest.mark.parametrize(
    ("model_id", "expected_efforts"),
    (
        ("claude-fable-5", {"low", "medium", "high", "xhigh", "max"}),
        ("claude-mythos-5", {"low", "medium", "high", "xhigh", "max"}),
        ("claude-opus-4-7", {"low", "medium", "high", "xhigh", "max"}),
        ("claude-opus-4-8", {"low", "medium", "high", "xhigh", "max"}),
        ("claude-sonnet-5", {"low", "medium", "high", "xhigh", "max"}),
        ("claude-sonnet-4-6", {"low", "medium", "high", "max"}),
    ),
)
def test_anthropic_reasoning_efforts_are_exact_model_policy(
    model_id: str,
    expected_efforts: set[str],
) -> None:
    profile = require_model_profile(
        ProviderModelRef(ANTHROPIC_PROVIDER_ID, model_id)
    )

    assert profile.supports(LLMCapability.REASONING_EFFORT)
    assert profile.reasoning_efforts == frozenset(expected_efforts)
    assert profile.default_reasoning_effort == "high"


def test_every_exact_anthropic_model_has_published_limits() -> None:
    for model_id in ANTHROPIC_EXACT_MODEL_IDS:
        profile = require_model_profile(
            ProviderModelRef(ANTHROPIC_PROVIDER_ID, model_id)
        )
        assert profile.context_window_tokens > 0
        assert profile.max_output_tokens > 0


@pytest.mark.parametrize(
    ("provider", "role", "expected_model"),
    [
        (OPENAI_PROVIDER_ID, ROLE_TOOL_OUTPUT_COMPRESSOR, "gpt-5-nano"),
        (OPENAI_PROVIDER_ID, ROLE_TOOL_CATEGORY_SELECTOR, "gpt-5-mini"),
        (OPENAI_PROVIDER_ID, ROLE_POST_TOOL_ARTICULATOR, "gpt-5-mini"),
        (ANTHROPIC_PROVIDER_ID, ROLE_TOOL_OUTPUT_COMPRESSOR, "claude-haiku-4-5-20251001"),
        (ANTHROPIC_PROVIDER_ID, ROLE_TOOL_CATEGORY_SELECTOR, "claude-haiku-4-5-20251001"),
        (ANTHROPIC_PROVIDER_ID, ROLE_POST_TOOL_ARTICULATOR, "claude-haiku-4-5-20251001"),
    ],
)
def test_provider_profiles_resolve_internal_role_models(
    provider: str,
    role: str,
    expected_model: str,
) -> None:
    ref = resolve_provider_internal_role_model(provider, role)

    assert ref == ProviderModelRef(provider, expected_model)


def test_anthropic_catalog_profiles_are_curated_exact_models() -> None:
    catalog_ids = tuple(
        profile.ref.model
        for profile in list_catalog_model_profiles(ANTHROPIC_PROVIDER_ID)
    )

    assert catalog_ids == tuple(sorted(ANTHROPIC_LISTABLE_MODEL_IDS))
    assert "claude-opus-4-8" in catalog_ids
    assert "claude-fable-5" in catalog_ids
    assert "claude-sonnet-5" in catalog_ids
    assert "claude-mythos-5" not in catalog_ids
    assert set(ANTHROPIC_NON_LISTABLE_MODEL_IDS) == {"claude-mythos-5"}


def test_compatibility_family_model_resolves_but_is_not_catalog_listable() -> None:
    profile = require_model_profile(_openai_ref("gpt-5-preview"))

    assert profile.ref == _openai_ref("gpt-5-preview")
    assert profile.listable is False
    assert profile.compatibility_family == "gpt-5"
    assert profile.context_window_tokens == DEFAULT_CONTEXT_WINDOW_TOKENS
    assert profile.max_output_tokens == OPENAI_RESPONSES_MAX_OUTPUT_TOKENS
    assert "gpt-5-preview" not in {
        catalog_profile.ref.model for catalog_profile in list_catalog_model_profiles()
    }


@pytest.mark.parametrize(
    ("model", "expected_max_output_tokens"),
    [
        ("gpt-5-preview", OPENAI_RESPONSES_MAX_OUTPUT_TOKENS),
        ("gpt-4.1", DEFAULT_MAX_OUTPUT_TOKENS),
        ("gpt-3.5-latest", DEFAULT_MAX_OUTPUT_TOKENS),
    ],
)
def test_compatibility_family_limits_resolve(
    model: str,
    expected_max_output_tokens: int,
) -> None:
    ref = _openai_ref(model)

    assert resolve_context_window_tokens(ref) == DEFAULT_CONTEXT_WINDOW_TOKENS
    assert resolve_max_output_tokens(ref) == expected_max_output_tokens


def test_unknown_profiles_fail_loudly() -> None:
    with pytest.raises(LLMProfileNotFoundError, match="No provider profile"):
        require_provider_profile("not-a-provider")

    with pytest.raises(LLMProfileNotFoundError, match="No model profile"):
        require_model_profile(ProviderModelRef("anthropic", "claude-unknown"))

    with pytest.raises(LLMProfileNotFoundError, match="No model profile"):
        require_model_profile(_openai_ref("text-davinci-003"))


def test_model_capability_checks_are_model_scoped() -> None:
    gpt5_profile = require_model_capability(_openai_ref("gpt-5.2"), LLMCapability.REASONING_EFFORT)

    assert gpt5_profile.ref == _openai_ref("gpt-5.2")
    assert supports_model(_openai_ref("gpt-5.2"), LLMCapability.REASONING_EFFORT)
    assert not supports_model(_openai_ref("gpt-4"), LLMCapability.REASONING_EFFORT)


def test_model_capability_checks_fail_loudly() -> None:
    with pytest.raises(LLMCapabilityNotSupportedError, match="does not support capability"):
        require_model_capability(_openai_ref("gpt-4"), LLMCapability.REASONING_EFFORT)


def test_reasoning_effort_profiles_are_model_scoped_for_xhigh() -> None:
    gpt52 = require_model_profile(_openai_ref("gpt-5.2"))
    xhigh_models = {
        "gpt-5.2-pro",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-5.4-pro",
        "gpt-5.5",
        "gpt-5.5-pro",
    }

    assert "xhigh" not in gpt52.reasoning_efforts
    assert gpt52.default_reasoning_effort == "minimal"
    for model_id in xhigh_models:
        profile = require_model_profile(_openai_ref(model_id))
        assert "xhigh" in profile.reasoning_efforts
    assert require_model_profile(_openai_ref("gpt-5.2-pro")).default_reasoning_effort == "minimal"
    assert require_model_profile(_openai_ref("gpt-5.4-pro")).default_reasoning_effort == "medium"
    assert require_model_profile(_openai_ref("gpt-5.5-pro")).default_reasoning_effort == "high"


@pytest.mark.parametrize(
    "model_id",
    ("gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
)
def test_gpt56_profiles_match_published_limits_and_reasoning(model_id: str) -> None:
    profile = require_model_profile(_openai_ref(model_id))

    assert profile.context_window_tokens == 1_050_000
    assert profile.max_output_tokens == 128_000
    assert profile.reasoning_efforts == frozenset(
        {"none", "low", "medium", "high", "xhigh", "max"}
    )
    assert profile.default_reasoning_effort == "medium"
    assert "minimal" not in profile.reasoning_efforts
    assert profile.listable is (model_id != "gpt-5.6")


def test_provider_neutral_contracts_are_reexported_from_package() -> None:
    from agent.providers.llm import (
        LLMCapability,
        ModelProfile,
        ProviderModelRef,
        ProviderProfile,
        STRUCTURED_OUTPUT_STRATEGIES,
        TOOL_CHOICE_MODES,
        get_default_model_ref,
        require_model_profile,
    )

    assert get_default_model_ref() == ProviderModelRef(OPENAI_PROVIDER_ID, "gpt-5.2")
    assert require_model_profile(_openai_ref("gpt-5.2")).supports(LLMCapability.CHAT)
    assert "native_schema" in STRUCTURED_OUTPUT_STRATEGIES
    assert "required" in TOOL_CHOICE_MODES
    assert ModelProfile is not None
    assert ProviderProfile is not None
