"""Tests for reviewed LLM catalog manifest loading and registry projection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.providers.llm.catalog.manifest_loader import (
    CatalogManifestValidationError,
    load_catalog_manifest,
)
from agent.providers.llm.core.capabilities import LLMCapability
from agent.providers.llm.core.identity import ANTHROPIC_PROVIDER_ID, OPENAI_PROVIDER_ID, ProviderModelRef
from agent.providers.llm.profiles.registry import (
    MODEL_PROFILE_REGISTRY,
    OPENAI_GPT_OSS_20B_MODEL_ID,
    list_model_profiles,
    require_model_profile,
)


def test_manifest_loads_one_active_revision_with_required_catalog_metadata() -> None:
    manifest = load_catalog_manifest()

    assert manifest.schema_version == 1
    assert manifest.active_revision
    assert manifest.active_revision == manifest.last_known_good_revision

    gpt_oss = manifest.require_model(OPENAI_PROVIDER_ID, OPENAI_GPT_OSS_20B_MODEL_ID)
    assert gpt_oss.canonical_model_id == "openai/gpt-oss-20b"
    assert gpt_oss.lifecycle == "active"
    assert gpt_oss.support_tier == "proving"
    assert gpt_oss.context_window_tokens > 0
    assert gpt_oss.max_output_tokens > 0
    assert LLMCapability.CHAT in gpt_oss.capabilities
    assert "openai-compatible-chat:gpt-oss-20b" in gpt_oss.aliases
    assert gpt_oss.pricing_schedule_ref == "unpriced:openai:gpt-oss-20b"


def test_registry_builds_immutable_profiles_from_reviewed_manifest() -> None:
    profile = require_model_profile(ProviderModelRef("OpenAI", "GPT-OSS-20B"))

    assert profile.canonical_model_id == "openai/gpt-oss-20b"
    assert profile.lifecycle == "active"
    assert profile.support_tier == "proving"
    assert profile.aliases == ("openai-compatible-chat:gpt-oss-20b",)
    assert profile.pricing_schedule_ref == "unpriced:openai:gpt-oss-20b"
    assert profile.pricing_provenance == "pricing_registry:openai_gpt_oss_pricing_not_registered"

    with pytest.raises(AttributeError):
        profile.lifecycle = "deprecated"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        MODEL_PROFILE_REGISTRY._models = {}  # type: ignore[misc]


def test_manifest_registry_projection_matches_exact_profile_inventory() -> None:
    manifest = load_catalog_manifest()
    manifest_refs = {
        (model.provider, model.model)
        for model in manifest.models
    }
    registry_refs = {
        (profile.ref.provider, profile.ref.model)
        for profile in list_model_profiles()
    }

    assert registry_refs == manifest_refs
    assert (OPENAI_PROVIDER_ID, "gpt-4o") in registry_refs
    assert (ANTHROPIC_PROVIDER_ID, "claude-sonnet-4-6") in registry_refs


def test_manifest_rejects_missing_capabilities_and_uses_last_known_good(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "active_revision": "candidate",
        "last_known_good_revision": "stable",
        "revisions": [
            {
                "revision": "candidate",
                "models": [
                    {
                        "provider": "openai",
                        "model": "broken",
                        "canonical_model_id": "openai:broken",
                        "display_name": "Broken",
                        "api_surface": "responses",
                        "lifecycle": "active",
                        "support_tier": "mainstream",
                        "limits": {
                            "context_window_tokens": 1,
                            "max_output_tokens": 1,
                        },
                        "listable": True,
                        "aliases": [],
                        "pricing_schedule_ref": "openai:broken",
                        "pricing_provenance": "unit-test",
                    }
                ],
            },
            {
                "revision": "stable",
                "models": [
                    {
                        "provider": "openai",
                        "model": "stable",
                        "canonical_model_id": "openai:stable",
                        "display_name": "Stable",
                        "api_surface": "responses",
                        "lifecycle": "active",
                        "support_tier": "mainstream",
                        "capabilities": ["chat", "context_window", "max_output_tokens"],
                        "limits": {
                            "context_window_tokens": 2,
                            "max_output_tokens": 1,
                        },
                        "listable": True,
                        "aliases": [],
                        "pricing_schedule_ref": "openai:stable",
                        "pricing_provenance": "unit-test",
                    }
                ],
            },
        ],
    }
    path = tmp_path / "catalog_manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    manifest = load_catalog_manifest(path)

    assert manifest.active_revision == "stable"
    assert manifest.require_model("openai", "stable").display_name == "Stable"

    payload["last_known_good_revision"] = "missing"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CatalogManifestValidationError):
        load_catalog_manifest(path)


def test_manifest_loader_does_not_scrape_provider_documentation() -> None:
    source = Path("agent/providers/llm/catalog/manifest_loader.py").read_text(encoding="utf-8")

    assert "requests." not in source
    assert "httpx." not in source
    assert "urllib.request" not in source
