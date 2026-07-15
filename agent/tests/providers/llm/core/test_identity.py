"""Tests for provider/model identity normalization and legacy resolution."""

from __future__ import annotations

import pytest

from agent.providers.llm.core.exceptions import LLMProviderNotFoundError
from agent.providers.llm.core.identity import (
    OPENAI_PROVIDER_ID,
    ProviderModelRef,
    get_openai_legacy_compatibility_family,
    normalize_model_id,
    normalize_provider_id,
    resolve_legacy_openai_model_ref,
)


def test_provider_model_ref_normalizes_lookup_identity() -> None:
    ref = ProviderModelRef(provider=" OpenAI ", model=" GPT-5.2 ")

    normalized = ref.normalized()

    assert normalized == ProviderModelRef(provider=OPENAI_PROVIDER_ID, model="gpt-5.2")
    assert str(ref) == "openai/gpt-5.2"


def test_provider_model_ref_rejects_empty_values() -> None:
    with pytest.raises(ValueError, match="provider cannot be empty"):
        ProviderModelRef(provider=" ", model="gpt-5.2")

    with pytest.raises(ValueError, match="model cannot be empty"):
        ProviderModelRef(provider=OPENAI_PROVIDER_ID, model="")


def test_normalization_helpers_reject_empty_values() -> None:
    with pytest.raises(ValueError, match="provider cannot be empty"):
        normalize_provider_id(" ")

    with pytest.raises(ValueError, match="model cannot be empty"):
        normalize_model_id("")


def test_legacy_openai_resolution_preserves_raw_request_model() -> None:
    resolution = resolve_legacy_openai_model_ref("GPT-5.2")

    assert resolution.lookup_ref == ProviderModelRef(OPENAI_PROVIDER_ID, "gpt-5.2")
    assert resolution.provider_request_model == "GPT-5.2"
    assert resolution.compatibility_family == "gpt-5"


def test_legacy_openai_resolution_supports_current_family_fallbacks() -> None:
    resolution = resolve_legacy_openai_model_ref("gpt-5-preview")

    assert resolution.lookup_ref == ProviderModelRef(OPENAI_PROVIDER_ID, "gpt-5-preview")
    assert resolution.provider_request_model == "gpt-5-preview"
    assert resolution.compatibility_family == "gpt-5"


@pytest.mark.parametrize(
    ("model", "expected_family"),
    [
        ("gpt-5-preview", "gpt-5"),
        ("gpt-4o-mini", "gpt-4"),
        ("gpt-4.1", "gpt-4"),
        ("gpt-3.5-turbo", "gpt-3.5"),
    ],
)
def test_openai_legacy_family_detection(model: str, expected_family: str) -> None:
    assert get_openai_legacy_compatibility_family(model) == expected_family


def test_legacy_openai_resolution_fails_for_unknown_model_family() -> None:
    with pytest.raises(LLMProviderNotFoundError, match="approved legacy OpenAI model family"):
        resolve_legacy_openai_model_ref("claude-3-5-sonnet")
