"""Tests for runtime manifest generation contract."""

from __future__ import annotations

from runtime_shared.runtime_manifest import (
    FILE_COMM_SCHEMA_VERSION,
    RUNTIME_CONTRACT_VERSION,
    SUPPORTED_TOOL_FAMILIES,
    WORKSPACE_LAYOUT_VERSION,
    build_runtime_manifest,
)


def test_runtime_manifest_defaults_include_required_contract_fields(monkeypatch) -> None:
    monkeypatch.delenv("DROWAI_RUNTIME_BUILD_REVISION", raising=False)
    monkeypatch.delenv("GIT_COMMIT", raising=False)
    monkeypatch.delenv("SOURCE_REVISION", raising=False)

    manifest = build_runtime_manifest()

    assert manifest.runtime_contract_version == RUNTIME_CONTRACT_VERSION
    assert manifest.source_revision == "unknown"
    assert manifest.file_comm_schema_version == FILE_COMM_SCHEMA_VERSION
    assert manifest.workspace_layout_version == WORKSPACE_LAYOUT_VERSION
    assert WORKSPACE_LAYOUT_VERSION == "2.0"
    assert manifest.supported_tool_families == SUPPORTED_TOOL_FAMILIES
    assert set(manifest.semantic_schema_versions) >= {"network", "web"}


def test_runtime_manifest_prefers_runtime_build_revision(monkeypatch) -> None:
    monkeypatch.setenv("DROWAI_RUNTIME_BUILD_REVISION", "rev-123")
    monkeypatch.setenv("GIT_COMMIT", "ignored")
    monkeypatch.setenv("SOURCE_REVISION", "ignored")

    manifest = build_runtime_manifest()

    assert manifest.source_revision == "rev-123"
