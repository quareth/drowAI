"""Regression tests for workspace archive and cleanup baseline behavior."""

import zipfile
from pathlib import Path

import pytest

from backend.config.workspace_config import WorkspaceConfig
from backend.services.workspace.manager import WorkspaceManager


@pytest.fixture
def isolated_workspaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Use a temporary workspaces root so tests do not touch repository state."""

    base = tmp_path / "workspaces"
    monkeypatch.setattr(
        WorkspaceConfig,
        "get_workspaces_base_path",
        staticmethod(lambda: base),
    )
    monkeypatch.setattr(
        WorkspaceConfig,
        "get_project_root",
        staticmethod(lambda: tmp_path),
    )
    return base


@pytest.mark.execution_plane_non_dind_regression
def test_workspace_structure(isolated_workspaces: Path) -> None:
    manager = WorkspaceManager()
    task_id = 123456
    path = Path(manager.create_workspace(task_id))

    assert (path / "commands.jsonl").exists()
    assert (path / "results.jsonl").exists()
    assert (path / "agent_state.json").exists()
    assert (path / "locks" / "commands.lock").exists()
    assert (path / "locks" / "results.lock").exists()

    manager.cleanup_workspace(task_id, archive_first=False)


def test_cleanup_removes_matching_control_root_only(
    isolated_workspaces: Path,
) -> None:
    manager = WorkspaceManager()
    first_id = 401
    second_id = 402
    manager.create_workspace(first_id)
    manager.create_workspace(second_id)
    first_control = WorkspaceConfig.ensure_control_structure(first_id)
    second_control = WorkspaceConfig.ensure_control_structure(second_id)

    assert manager.cleanup_workspace(first_id, archive_first=False) is True

    assert not first_control.exists()
    assert second_control.exists()


def test_local_legacy_runtime_input_migrates_to_secured_control_root(
    isolated_workspaces: Path,
) -> None:
    manager = WorkspaceManager()
    task_id = 403
    workspace = Path(manager.create_workspace(task_id))
    legacy = workspace / "user_input.jsonl"
    legacy.write_bytes(b'{"message":"continue"}\n')

    WorkspaceConfig.migrate_legacy_runtime_input(task_id)

    control_input = (
        WorkspaceConfig.get_task_control_path(task_id)
        / "runtime-input"
        / "user_input.jsonl"
    )
    assert control_input.read_bytes() == legacy.read_bytes()
    assert control_input.stat().st_mode & 0o777 == 0o600

    WorkspaceConfig.finalize_legacy_control_cutover(task_id)
    assert not legacy.exists()


def test_workspace_archive_excludes_control_material(
    isolated_workspaces: Path,
) -> None:
    manager = WorkspaceManager()
    task_id = 404
    workspace = Path(manager.create_workspace(task_id))
    (workspace / "report.txt").write_text("visible", encoding="utf-8")
    control = WorkspaceConfig.ensure_control_structure(task_id)
    (control / "vpn" / "task.ovpn").write_text("secret", encoding="utf-8")

    archive_path = Path(manager.archive_workspace(task_id))

    with zipfile.ZipFile(archive_path) as archive:
        assert "report.txt" in archive.namelist()
        assert all("task.ovpn" not in name for name in archive.namelist())


def test_cleanup_workspace_archives_then_deletes_when_archive_first_enabled(
    isolated_workspaces: Path,
) -> None:
    manager = WorkspaceManager()
    task_id = 98765
    workspace = Path(manager.create_workspace(task_id))
    evidence = workspace / "artifacts" / "evidence.txt"
    evidence.write_text("phase0-regression", encoding="utf-8")

    cleaned = manager.cleanup_workspace(task_id, archive_first=True)

    assert cleaned is True
    assert not workspace.exists()

    archives_dir = isolated_workspaces / "archives"
    archives = list(archives_dir.glob(f"task_{task_id}_workspace_*.zip"))
    assert len(archives) == 1

    with zipfile.ZipFile(archives[0]) as archive:
        assert "artifacts/evidence.txt" in archive.namelist()


def test_cleanup_workspace_archives_to_engagement_owned_fallback_path(
    isolated_workspaces: Path,
) -> None:
    manager = WorkspaceManager()
    task_id = 87654
    engagement_id = 321
    workspace = Path(manager.create_workspace(task_id))
    evidence = workspace / "artifacts" / "evidence.txt"
    evidence.write_text("fallback-archive", encoding="utf-8")

    cleaned = manager.cleanup_workspace(task_id, archive_first=True, engagement_id=engagement_id)

    assert cleaned is True
    assert not workspace.exists()

    durable_archives = WorkspaceConfig.get_engagement_workspace_archives_path(engagement_id)
    archives = list(durable_archives.glob(f"task_{task_id}_workspace_*.zip"))
    assert len(archives) == 1
    with zipfile.ZipFile(archives[0]) as archive:
        assert "artifacts/evidence.txt" in archive.namelist()


def test_archive_workspace_rejects_symlink_descendant_without_reading_target(
    isolated_workspaces: Path,
    tmp_path: Path,
) -> None:
    manager = WorkspaceManager()
    task_id = 76543
    workspace = Path(manager.create_workspace(task_id))
    outside = tmp_path / "outside-canary.txt"
    outside.write_text("HOST_ONLY_CANARY", encoding="utf-8")
    (workspace / "artifacts" / "linked-secret.txt").symlink_to(outside)

    with pytest.raises(ValueError, match="symlink|unsafe"):
        manager.archive_workspace(task_id)

    assert outside.read_text(encoding="utf-8") == "HOST_ONLY_CANARY"
