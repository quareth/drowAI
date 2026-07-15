"""Tests for runner/image workspace contract alignment."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from agent.workspace_init import init_workspace
from drowai_runner.workspace import RunnerWorkspaceManager, initialize_task_workspace
from runtime_shared.file_comm_contracts import (
    LOCKS_DIRECTORY_NAME,
    STANDARD_LOCK_FILES,
    STANDARD_RUNTIME_FILES,
    STANDARD_RUNTIME_SUBDIRECTORIES,
)


def _relative_tree(root: Path) -> set[str]:
    return {
        str(path.relative_to(root))
        for path in root.rglob("*")
    }


def test_runner_workspace_initializer_matches_image_initializer(tmp_path: Path) -> None:
    image_workspace = tmp_path / "image"
    runner_workspace = tmp_path / "runner"

    init_workspace(str(image_workspace))
    initialize_task_workspace(runner_workspace)

    expected_subdirectories = {LOCKS_DIRECTORY_NAME, *STANDARD_RUNTIME_SUBDIRECTORIES}
    expected_files = set(STANDARD_RUNTIME_FILES) | {
        f"{LOCKS_DIRECTORY_NAME}/{name}" for name in STANDARD_LOCK_FILES
    }

    image_tree = _relative_tree(image_workspace)
    runner_tree = _relative_tree(runner_workspace)
    for path in expected_subdirectories | expected_files:
        assert path in image_tree
        assert path in runner_tree


def test_workspace_manager_initializes_task_layout_with_shared_constants(
    tmp_path: Path,
) -> None:
    manager = RunnerWorkspaceManager(tmp_path / "runner-root")

    workspace = manager.initialize_task_workspace("task-17")

    assert workspace == manager.tasks_root / "task-17"
    assert (manager.runner_root / "runner.json").exists()
    assert manager.tasks_root.exists()
    for subdirectory in STANDARD_RUNTIME_SUBDIRECTORIES:
        assert (workspace / subdirectory).exists()
    for file_name in STANDARD_RUNTIME_FILES:
        assert (workspace / file_name).exists()
    assert (workspace / "scope.md").exists()
    assert (workspace / "config.json").exists()
    assert (workspace / "reports").exists()
    control = manager.resolve_task_control("task-17")
    assert stat.S_IMODE(control.stat().st_mode) == 0o700
    assert (control / "vpn").exists()
    assert (control / "runtime-input" / "user_input.jsonl").exists()
    assert not (workspace / "vpn").exists()


def test_workspace_manager_rejects_workspace_path_traversal(tmp_path: Path) -> None:
    manager = RunnerWorkspaceManager(tmp_path / "runner-root")

    with pytest.raises(ValueError, match="single path segment"):
        manager.resolve_task_workspace("../task-1")
    with pytest.raises(ValueError, match="single path segment"):
        manager.resolve_task_workspace("nested/workspace")
    with pytest.raises(ValueError, match="must not start with '-'"):
        manager.resolve_task_workspace("-opaque-id")


def test_workspace_manager_accepts_opaque_workspace_ids(tmp_path: Path) -> None:
    manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    workspace = manager.initialize_task_workspace("ws_opaque-123")

    assert workspace == manager.tasks_root / "ws_opaque-123"
    assert workspace.exists()


def test_workspace_cleanup_removes_only_target_task(tmp_path: Path) -> None:
    manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    first = manager.initialize_task_workspace("task-1")
    second = manager.initialize_task_workspace("task-2")
    first_control = manager.resolve_task_control("task-1")
    second_control = manager.resolve_task_control("task-2")

    manager.cleanup_task_workspace("task-1")

    assert not first.exists()
    assert not first_control.exists()
    assert second.exists()
    assert second_control.exists()


def test_legacy_runtime_input_migrates_then_is_removed_after_cutover(
    tmp_path: Path,
) -> None:
    manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    workspace = manager.initialize_task_workspace("task-legacy")
    (workspace / "user_input.jsonl").write_bytes(b'{"message":"continue"}\n')
    legacy_vpn = workspace / "vpn" / "task-77.ovpn"
    legacy_vpn.parent.mkdir()
    legacy_vpn.write_text("legacy-vpn", encoding="utf-8")
    credentials = legacy_vpn.parent / "credentials.txt"
    credentials.write_text("user\npass\n", encoding="utf-8")
    user_data = workspace / "report.txt"
    user_data.write_text("keep", encoding="utf-8")

    manager.migrate_legacy_runtime_input("task-legacy")

    control_input = (
        manager.resolve_task_control("task-legacy")
        / "runtime-input"
        / "user_input.jsonl"
    )
    assert control_input.read_bytes() == b'{"message":"continue"}\n'
    assert stat.S_IMODE(control_input.stat().st_mode) == 0o600

    manager.finalize_legacy_control_cutover("task-legacy")

    assert not (workspace / "user_input.jsonl").exists()
    assert not legacy_vpn.exists()
    assert credentials.exists()
    assert user_data.read_text(encoding="utf-8") == "keep"
    assert control_input.read_bytes() == b'{"message":"continue"}\n'


def test_unsafe_legacy_runtime_input_is_rejected_with_empty_control_file(
    tmp_path: Path,
) -> None:
    manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    workspace = manager.initialize_task_workspace("task-unsafe-legacy")
    canary = tmp_path / "outside.jsonl"
    canary.write_bytes(b"canary\n")
    (workspace / "user_input.jsonl").symlink_to(canary)

    with pytest.warns(RuntimeWarning, match="Unsafe legacy runtime input"):
        manager.migrate_legacy_runtime_input("task-unsafe-legacy")

    control_input = (
        manager.resolve_task_control("task-unsafe-legacy")
        / "runtime-input"
        / "user_input.jsonl"
    )
    assert control_input.read_bytes() == b""
    assert canary.read_bytes() == b"canary\n"


def test_vpn_files_are_task_local_and_permission_restricted(tmp_path: Path) -> None:
    manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    manager.initialize_task_workspace("task-5")

    vpn_file = manager.write_vpn_file("task-5", "client.ovpn", "vpn-config")
    vpn_directory = vpn_file.parent

    assert vpn_directory == manager.resolve_task_control("task-5") / "vpn"
    assert stat.S_IMODE(vpn_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(vpn_file.stat().st_mode) == 0o600


def test_vpn_write_replaces_destination_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    manager.initialize_task_workspace("task-5")
    canary = tmp_path / "outside.txt"
    canary.write_text("unchanged", encoding="utf-8")
    destination = manager.resolve_task_control("task-5") / "vpn" / "client.ovpn"
    destination.symlink_to(canary)

    written = manager.write_vpn_file("task-5", "client.ovpn", "vpn-config")

    assert canary.read_text(encoding="utf-8") == "unchanged"
    assert not written.is_symlink()
    assert written.read_text(encoding="utf-8") == "vpn-config"


def test_vpn_write_rejects_symlinked_parent_directory(tmp_path: Path) -> None:
    manager = RunnerWorkspaceManager(tmp_path / "runner-root")
    manager.initialize_task_workspace("task-5")
    outside = tmp_path / "outside"
    outside.mkdir()
    vpn_directory = manager.resolve_task_control("task-5") / "vpn"
    vpn_directory.rmdir()
    vpn_directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="unsafe"):
        manager.write_vpn_file("task-5", "client.ovpn", "vpn-config")

    assert list(outside.iterdir()) == []
