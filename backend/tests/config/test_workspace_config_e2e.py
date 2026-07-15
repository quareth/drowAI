"""Tests for process-gated E2E workspace root isolation."""

from __future__ import annotations

from backend.config.workspace_config import WorkspaceConfig


_ORIGINAL_DURABLE_ROOT = WorkspaceConfig.get_durable_knowledge_base_path


def test_e2e_roots_apply_only_in_explicit_e2e_modes(monkeypatch, tmp_path) -> None:
    workspace_root = tmp_path / "workspace"
    durable_root = tmp_path / "durable"
    monkeypatch.setenv("E2E_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("E2E_DURABLE_KNOWLEDGE_ROOT", str(durable_root))

    monkeypatch.delenv("E2E_DETERMINISTIC_MODE", raising=False)
    monkeypatch.delenv("E2E_RUNTIME_LOCAL_MODE", raising=False)
    assert WorkspaceConfig.get_workspaces_base_path() != workspace_root
    assert _ORIGINAL_DURABLE_ROOT() != durable_root

    monkeypatch.setenv("E2E_DETERMINISTIC_MODE", "true")
    assert WorkspaceConfig.get_workspaces_base_path() == workspace_root
    assert _ORIGINAL_DURABLE_ROOT() == durable_root

    monkeypatch.setenv("E2E_DETERMINISTIC_MODE", "false")
    monkeypatch.setenv("E2E_RUNTIME_LOCAL_MODE", "true")
    assert WorkspaceConfig.get_workspaces_base_path() == workspace_root
    assert _ORIGINAL_DURABLE_ROOT() == durable_root
