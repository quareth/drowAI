"""Tests for GPT-OSS 20B curated model profile metadata."""

from __future__ import annotations

from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID, ProviderModelRef
from agent.providers.llm.profiles.registry import (
    OPENAI_API_SURFACE_CHAT_COMPLETIONS,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    list_catalog_model_profiles,
    require_model_profile,
)


def test_gpt_oss_20b_profile_uses_conservative_canonical_metadata() -> None:
    profile = require_model_profile(ProviderModelRef(OPENAI_PROVIDER_ID, "gpt-oss-20b"))

    assert str(profile.ref) == "openai/gpt-oss-20b"
    assert profile.display_name == "GPT-OSS 20B"
    assert profile.api_surface == OPENAI_API_SURFACE_CHAT_COMPLETIONS
    assert profile.listable is True
    assert profile.context_window_tokens == DEFAULT_CONTEXT_WINDOW_TOKENS
    assert profile.max_output_tokens == DEFAULT_MAX_OUTPUT_TOKENS
    assert profile.capabilities == frozenset(
        {
            LLMCapability.CHAT,
            LLMCapability.USAGE_REPORTING,
            LLMCapability.CONTEXT_WINDOW,
            LLMCapability.MAX_OUTPUT_TOKENS,
        }
    )
    assert not profile.supports(LLMCapability.STREAMING)
    assert not profile.supports(LLMCapability.TOOLS)
    assert not profile.supports(LLMCapability.PARALLEL_TOOLS)
    assert not profile.supports(LLMCapability.STRUCTURED_OUTPUT_NATIVE)
    assert not profile.supports(LLMCapability.REASONING_EFFORT)
    assert profile.reasoning_efforts == frozenset()
    assert profile.tool_choice_modes == frozenset()
    assert profile.structured_output_strategies == frozenset()


def test_gpt_oss_20b_is_public_catalog_metadata() -> None:
    catalog_ids = {
        str(profile.ref) for profile in list_catalog_model_profiles(OPENAI_PROVIDER_ID)
    }

    assert "openai/gpt-oss-20b" in catalog_ids
