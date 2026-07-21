"""Deployment baseline tests for provider profile registry behavior."""

from __future__ import annotations

import pytest

from agent.providers.llm.core.exceptions import LLMProfileNotFoundError
from agent.providers.llm.core.identity import (
    ANTHROPIC_PROVIDER_ID,
    OPENAI_PROVIDER_ID,
    ProviderModelRef,
)
from agent.providers.llm.profiles.registry import (
    ANTHROPIC_API_SURFACE_MESSAGES,
    ANTHROPIC_DEFAULT_MODEL_ID,
    ANTHROPIC_EXACT_MODEL_IDS,
    ANTHROPIC_LISTABLE_MODEL_IDS,
    ANTHROPIC_NON_LISTABLE_MODEL_IDS,
    OPENAI_API_SURFACE_CHAT_COMPLETIONS,
    OPENAI_API_SURFACE_RESPONSES,
    OPENAI_DEFAULT_MODEL_ID,
    OPENAI_EXACT_MODEL_IDS,
    OPENAI_GPT_OSS_20B_MODEL_ID,
    OPENAI_LEGACY_CHAT_MODEL_IDS,
    OPENAI_LISTABLE_MODEL_IDS,
    get_default_model_ref,
    get_provider_default_model_ref,
    list_catalog_model_profiles,
    list_model_profiles,
    require_model_profile,
)


def _ref(provider: str, model: str) -> ProviderModelRef:
    return ProviderModelRef(provider, model)


def _catalog_ids(provider: str) -> set[str]:
    return {
        profile.ref.model
        for profile in list_catalog_model_profiles(provider)
    }


def test_openai_profiles_preserve_default_listability_and_api_surfaces() -> None:
    exact_profiles = {
        profile.ref.model: profile
        for profile in list_model_profiles(provider_id=OPENAI_PROVIDER_ID)
    }

    assert get_default_model_ref() == _ref(
        OPENAI_PROVIDER_ID,
        OPENAI_DEFAULT_MODEL_ID,
    )
    assert get_provider_default_model_ref(OPENAI_PROVIDER_ID) == _ref(
        OPENAI_PROVIDER_ID,
        OPENAI_DEFAULT_MODEL_ID,
    )
    assert set(exact_profiles) == set(OPENAI_EXACT_MODEL_IDS)
    assert _catalog_ids(OPENAI_PROVIDER_ID) == set(OPENAI_LISTABLE_MODEL_IDS)

    for model_id in OPENAI_LISTABLE_MODEL_IDS:
        profile = exact_profiles[model_id]
        expected_surface = (
            OPENAI_API_SURFACE_CHAT_COMPLETIONS
            if model_id == OPENAI_GPT_OSS_20B_MODEL_ID
            else OPENAI_API_SURFACE_RESPONSES
        )
        assert profile.api_surface == expected_surface
        assert profile.listable is True
        assert profile.compatibility_family is None

    for model_id in OPENAI_LEGACY_CHAT_MODEL_IDS:
        profile = exact_profiles[model_id]
        assert profile.api_surface == OPENAI_API_SURFACE_CHAT_COMPLETIONS
        assert profile.listable is False
        assert profile.compatibility_family is None
        assert model_id not in _catalog_ids(OPENAI_PROVIDER_ID)


def test_anthropic_profiles_preserve_default_listability_and_messages_surface() -> None:
    exact_profiles = {
        profile.ref.model: profile
        for profile in list_model_profiles(provider_id=ANTHROPIC_PROVIDER_ID)
    }

    assert get_provider_default_model_ref(ANTHROPIC_PROVIDER_ID) == _ref(
        ANTHROPIC_PROVIDER_ID,
        ANTHROPIC_DEFAULT_MODEL_ID,
    )
    assert set(exact_profiles) == set(ANTHROPIC_EXACT_MODEL_IDS)
    assert _catalog_ids(ANTHROPIC_PROVIDER_ID) == set(ANTHROPIC_LISTABLE_MODEL_IDS)

    for model_id in ANTHROPIC_LISTABLE_MODEL_IDS:
        profile = exact_profiles[model_id]
        assert profile.api_surface == ANTHROPIC_API_SURFACE_MESSAGES
        assert profile.listable is True
        assert profile.compatibility_family is None

    for model_id in ANTHROPIC_NON_LISTABLE_MODEL_IDS:
        profile = exact_profiles[model_id]
        assert profile.api_surface == ANTHROPIC_API_SURFACE_MESSAGES
        assert profile.listable is False
        assert profile.compatibility_family is None
        assert model_id not in _catalog_ids(ANTHROPIC_PROVIDER_ID)


@pytest.mark.parametrize(
    ("model_id", "compatibility_family", "api_surface"),
    (
        ("gpt-5-preview", "gpt-5", OPENAI_API_SURFACE_RESPONSES),
        ("gpt-4.1", "gpt-4", OPENAI_API_SURFACE_CHAT_COMPLETIONS),
        ("gpt-3.5-latest", "gpt-3.5", OPENAI_API_SURFACE_CHAT_COMPLETIONS),
    ),
)
def test_openai_legacy_prefix_compatibility_is_profile_lookup_only(
    model_id: str,
    compatibility_family: str,
    api_surface: str,
) -> None:
    profile = require_model_profile(_ref(OPENAI_PROVIDER_ID, model_id))

    assert profile.ref == _ref(OPENAI_PROVIDER_ID, model_id)
    assert profile.compatibility_family == compatibility_family
    assert profile.api_surface == api_surface
    assert profile.listable is False
    assert model_id not in {
        profile.ref.model
        for profile in list_model_profiles(provider_id=OPENAI_PROVIDER_ID)
    }
    assert model_id not in _catalog_ids(OPENAI_PROVIDER_ID)


def test_unknown_provider_and_models_still_fail_closed() -> None:
    with pytest.raises(
        LLMProfileNotFoundError,
        match="No default model registered",
    ):
        get_provider_default_model_ref("mistral")

    with pytest.raises(LLMProfileNotFoundError, match="No model profile"):
        require_model_profile(_ref(OPENAI_PROVIDER_ID, "text-davinci-003"))

    with pytest.raises(LLMProfileNotFoundError, match="No model profile"):
        require_model_profile(_ref(ANTHROPIC_PROVIDER_ID, "claude-unknown"))
