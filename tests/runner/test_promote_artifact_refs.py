"""Tests for runner promote_artifact_refs operation."""

from __future__ import annotations

from pathlib import Path

from drowai_runner.operation_service import RunnerOperationService


def test_promote_artifact_refs_scans_explicit_refs_only(tmp_path: Path) -> None:
    workspace_id = "task-promote-1"
    task_root = tmp_path / workspace_id
    artifacts_dir = task_root / "artifacts"
    artifacts_dir.mkdir(parents=True)
    artifact_path = artifacts_dir / "fping_123.txt"
    artifact_path.write_text("172.17.0.1 is alive\n", encoding="utf-8")
    unrelated = task_root / "notes.txt"
    unrelated.write_text("ignore me", encoding="utf-8")

    class _Workspace:
        def resolve_task_workspace(self, requested_workspace_id: str) -> Path:
            assert requested_workspace_id == workspace_id
            return task_root

    service = RunnerOperationService.__new__(RunnerOperationService)
    service._workspace = _Workspace()
    response = service._promote_artifact_refs(
        {
            "workspace_id": workspace_id,
            "artifacts": ["artifacts/fping_123.txt", "notes.txt"],
        }
    )
    assert response["accepted"] is True
    metadata = response["metadata"]
    assert metadata["artifacts"] == ["artifacts/fping_123.txt", "notes.txt"]
    manifest = metadata["artifact_manifest"]
    assert manifest["declared_count"] == 2
    assert manifest["accepted_count"] == 2
