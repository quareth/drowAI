"""Runner-local task workspace initialization helpers.

This module initializes managed runner task workspaces using the shared
runtime file-comm/workspace filename contracts so runner and runtime-image
workspaces stay aligned.
"""

from __future__ import annotations

import os
from pathlib import Path
import warnings

from runtime_shared.file_comm_contracts import (
    LOCKS_DIRECTORY_NAME,
    STANDARD_LOCK_FILES,
    STANDARD_RUNTIME_FILES,
    STANDARD_RUNTIME_SUBDIRECTORIES,
)
from runtime_shared.workspace_filesystem import (
    WorkspaceEntryUnsafeError,
    WorkspaceFilesystem,
)

_RUNNER_METADATA_FILE = "runner.json"
_TASKS_DIRECTORY = "tasks"
_CONTROL_DIRECTORY = "control"
_TASK_CONFIG_FILE = "config.json"
_TASK_SCOPE_FILE = "scope.md"
_VPN_DIRECTORY = "vpn"
_RUNNER_ONLY_SUBDIRECTORIES: tuple[str, ...] = ("reports",)


class RunnerWorkspaceManager:
    """Own runner-local path resolution, task initialization, and cleanup."""

    def __init__(self, runner_root: str | Path) -> None:
        self._runner_root = Path(runner_root)
        self._tasks_root = self._runner_root / _TASKS_DIRECTORY
        self._control_root = self._runner_root / _CONTROL_DIRECTORY

    @property
    def runner_root(self) -> Path:
        """Return the configured runner root path."""
        return self._runner_root

    @property
    def tasks_root(self) -> Path:
        """Return the root directory that contains task workspaces."""
        return self._tasks_root

    def initialize_runner_root(self) -> Path:
        """Create stable top-level runner files and directories."""
        self._runner_root.mkdir(parents=True, exist_ok=True)
        self._tasks_root.mkdir(parents=True, exist_ok=True, mode=0o755)
        self._control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._control_root, 0o700)
        runner_metadata_path = self._runner_root / _RUNNER_METADATA_FILE
        if not runner_metadata_path.exists():
            runner_metadata_path.write_text("{}\n", encoding="utf-8")
        os.chmod(runner_metadata_path, 0o644)
        return self._runner_root

    def resolve_task_workspace(self, workspace_id: str) -> Path:
        """Resolve a workspace id to a task-local path with strict validation."""
        cleaned = workspace_id.strip()
        if not cleaned:
            raise ValueError("workspace_id must not be empty.")
        if cleaned in {".", ".."} or "/" in cleaned or "\\" in cleaned:
            raise ValueError("workspace_id must be a single path segment.")
        if cleaned.startswith("-"):
            raise ValueError("workspace_id must not start with '-'.")
        return self._tasks_root / cleaned

    def resolve_task_control(self, workspace_id: str) -> Path:
        """Resolve one workspace identity to its host-owned control root."""
        self.resolve_task_workspace(workspace_id)
        return self._control_root / workspace_id.strip()

    def initialize_task_control(self, workspace_id: str) -> Path:
        """Create one secured task control root and canonical files."""
        self.initialize_runner_root()
        control = self.resolve_task_control(workspace_id)
        control.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(control, 0o700)
        filesystem = WorkspaceFilesystem(control)
        filesystem.mkdirs("vpn", mode=0o700)
        filesystem.mkdirs("runtime-input", mode=0o700)
        self._ensure_regular_file(
            filesystem, "runtime-input/user_input.jsonl", mode=0o600
        )
        filesystem.chmod_file("runtime-input/user_input.jsonl", 0o600)
        return control

    def migrate_legacy_runtime_input(self, workspace_id: str) -> None:
        """Copy a safe legacy runtime-input file into control before recreation."""
        control = self.control_filesystem(workspace_id)
        data_filesystem = self.filesystem(workspace_id)
        try:
            legacy_content = data_filesystem.read_bytes("user_input.jsonl")
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            warnings.warn(
                "Unsafe legacy runtime input was rejected during control migration.",
                RuntimeWarning,
                stacklevel=2,
            )
            return
        control.write_bytes_atomic(
            "runtime-input/user_input.jsonl", legacy_content, mode=0o600
        )

    def finalize_legacy_control_cutover(self, workspace_id: str) -> None:
        """Remove legacy data entries after the recreated runtime is verified."""
        filesystem = self.filesystem(workspace_id)
        filesystem.remove("user_input.jsonl", missing_ok=True)
        try:
            vpn_entries = filesystem.list_entries("vpn", recursive=False)
        except FileNotFoundError:
            return
        except WorkspaceEntryUnsafeError:
            filesystem.remove("vpn", recursive=True, missing_ok=True)
            return
        for entry in vpn_entries:
            if entry.kind == "file" and entry.relative_path.endswith(".ovpn"):
                filesystem.remove(entry.relative_path, missing_ok=True)

    def initialize_task_workspace(self, workspace_id: str) -> Path:
        """Create one task workspace with runtime and runner-owned directories."""
        self.initialize_runner_root()
        workspace = self.resolve_task_workspace(workspace_id)
        workspace.mkdir(parents=True, exist_ok=True, mode=0o755)
        filesystem = WorkspaceFilesystem(workspace)
        self.initialize_task_control(workspace_id)

        for subdirectory in STANDARD_RUNTIME_SUBDIRECTORIES:
            filesystem.mkdirs(subdirectory, mode=0o755)
        for subdirectory in _RUNNER_ONLY_SUBDIRECTORIES:
            filesystem.mkdirs(subdirectory, mode=0o755)

        for file_name in STANDARD_RUNTIME_FILES:
            self._ensure_regular_file(filesystem, file_name, mode=0o644)
        for lock_name in STANDARD_LOCK_FILES:
            self._ensure_regular_file(
                filesystem, f"{LOCKS_DIRECTORY_NAME}/{lock_name}", mode=0o644
            )

        self._ensure_regular_file(filesystem, _TASK_SCOPE_FILE, mode=0o644)
        self._ensure_regular_file(
            filesystem, _TASK_CONFIG_FILE, content=b"{}\n", mode=0o644
        )

        return workspace

    def write_vpn_file(self, workspace_id: str, file_name: str, content: str) -> Path:
        """Write a task-local VPN file with restrictive permissions."""
        target = Path(file_name)
        if target.name != file_name or file_name in {"", ".", ".."}:
            raise ValueError("VPN file name must be a plain file name.")
        filesystem = self.control_filesystem(workspace_id)
        filesystem.mkdirs(_VPN_DIRECTORY, mode=0o700)
        relative_path = f"{_VPN_DIRECTORY}/{file_name}"
        filesystem.write_bytes_atomic(relative_path, content.encode("utf-8"), mode=0o600)
        return self.resolve_task_control(workspace_id) / relative_path

    def control_filesystem(self, workspace_id: str) -> WorkspaceFilesystem:
        """Return the safe filesystem capability for one task control root."""
        return WorkspaceFilesystem(self.initialize_task_control(workspace_id))

    def filesystem(self, workspace_id: str) -> WorkspaceFilesystem:
        """Return the race-safe filesystem capability for one task workspace."""
        return WorkspaceFilesystem(self.resolve_task_workspace(workspace_id))

    def read_workspace_bytes(
        self, workspace_id: str, relative_path: str, *, max_bytes: int | None = None
    ) -> bytes:
        """Read a regular task-workspace file without following symlinks."""
        return self.filesystem(workspace_id).read_bytes(relative_path, max_bytes=max_bytes)

    def write_workspace_bytes(
        self, workspace_id: str, relative_path: str, content: bytes, *, mode: int = 0o600
    ) -> None:
        """Atomically replace a task-workspace file without following symlinks."""
        self.filesystem(workspace_id).write_bytes_atomic(relative_path, content, mode=mode)

    def append_workspace_bytes(
        self, workspace_id: str, relative_path: str, content: bytes, *, mode: int = 0o600
    ) -> None:
        """Append to a regular task-workspace file without following symlinks."""
        self.filesystem(workspace_id).append_bytes(relative_path, content, mode=mode)

    @staticmethod
    def _ensure_regular_file(
        filesystem: WorkspaceFilesystem,
        relative_path: str,
        *,
        content: bytes = b"",
        mode: int,
    ) -> None:
        try:
            filesystem.metadata(relative_path)
        except FileNotFoundError:
            filesystem.write_bytes_atomic(relative_path, content, mode=mode)

    def cleanup_task_workspace(self, workspace_id: str) -> None:
        """Remove one task workspace only, leaving sibling tasks untouched."""
        self.initialize_runner_root()
        WorkspaceFilesystem(self._tasks_root).remove(
            workspace_id, recursive=True, missing_ok=True
        )
        WorkspaceFilesystem(self._control_root).remove(
            workspace_id, recursive=True, missing_ok=True
        )


def initialize_task_workspace(task_workspace: str | Path) -> Path:
    """Create the standard runtime workspace layout for one runner task.

    This compatibility helper preserves direct path initialization for tests
    and transitional callers that do not yet use `RunnerWorkspaceManager`.
    """
    workspace = Path(task_workspace)
    workspace.mkdir(parents=True, exist_ok=True, mode=0o755)
    filesystem = WorkspaceFilesystem(workspace)

    for subdirectory in STANDARD_RUNTIME_SUBDIRECTORIES:
        filesystem.mkdirs(subdirectory, mode=0o755)

    for file_name in STANDARD_RUNTIME_FILES:
        RunnerWorkspaceManager._ensure_regular_file(
            filesystem, file_name, mode=0o644
        )

    for lock_name in STANDARD_LOCK_FILES:
        RunnerWorkspaceManager._ensure_regular_file(
            filesystem, f"{LOCKS_DIRECTORY_NAME}/{lock_name}", mode=0o644
        )

    return workspace
