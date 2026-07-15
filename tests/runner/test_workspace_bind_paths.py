"""Tests for host-visible workspace bind path resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime_shared.workspace_bind_paths import resolve_workspace_bind_source


def test_resolve_workspace_bind_source_without_host_root_uses_workspace_path(
    tmp_path: Path,
) -> None:
    runner_root = tmp_path / "runner"
    workspace = runner_root / "tasks" / "task-1"
    workspace.mkdir(parents=True)

    bind_source = resolve_workspace_bind_source(
        workspace,
        runner_root=runner_root,
        host_bind_root=None,
    )

    assert bind_source == str(workspace.resolve())


def test_resolve_workspace_bind_source_maps_to_host_bind_root(tmp_path: Path) -> None:
    runner_root = tmp_path / "container" / "data"
    host_root = tmp_path / "host" / "data"
    workspace = runner_root / "tasks" / "task-9"
    workspace.mkdir(parents=True)

    bind_source = resolve_workspace_bind_source(
        workspace,
        runner_root=runner_root,
        host_bind_root=host_root,
    )

    assert bind_source == str((host_root / "tasks" / "task-9").resolve())


def test_resolve_workspace_bind_source_rejects_outside_runner_root(tmp_path: Path) -> None:
    runner_root = tmp_path / "runner"
    workspace = tmp_path / "outside" / "task-1"
    workspace.mkdir(parents=True)

    with pytest.raises(ValueError, match="must be under runner_root"):
        resolve_workspace_bind_source(
            workspace,
            runner_root=runner_root,
            host_bind_root=tmp_path / "host",
        )
