"""Tests for GPT-OSS 20B catalog projection metadata."""

from __future__ import annotations

from agent.providers.llm.catalog.manifest_loader import load_catalog_manifest
from backend.services.llm_provider.catalog_service import LLMProviderCatalogService


def test_gpt_oss_manifest_uses_normalized_canonical_model_id() -> None:
    manifest = load_catalog_manifest()
    gpt_oss = manifest.require_model("openai", "gpt-oss-20b")

    assert gpt_oss.canonical_model_id == "openai/gpt-oss-20b"


def test_gpt_oss_catalog_projection_separates_canonical_and_wire_ids() -> None:
    catalog = LLMProviderCatalogService()
    openai_provider = next(
        provider for provider in catalog.list_providers() if provider.id == "openai"
    )
    gpt_oss = next(model for model in openai_provider.models if model.id == "gpt-oss-20b")

    assert gpt_oss.canonical_model_id == "openai/gpt-oss-20b"
    assert gpt_oss.exact_wire_model_id is None
    assert gpt_oss.pricing_status == "unavailable"
    assert gpt_oss.api_surface == "chat_completions"
    assert set(gpt_oss.capabilities) == {
        "chat",
        "context_window",
        "max_output_tokens",
        "streaming",
        "streaming_usage_reporting",
        "structured_output_native",
        "tools",
        "usage_reporting",
    }
