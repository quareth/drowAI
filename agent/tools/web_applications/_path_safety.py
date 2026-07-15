"""Workspace path helpers for web application tool wrappers.

This module owns only workspace-safe path resolution for web application tools.
Tool-specific command semantics stay in each tool wrapper.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from agent.utils.workspace_helpers import resolve_container_path

from ..filesystem._helpers import (
    resolve_workspace_path_safe,
    to_workspace_relative,
    workspace_root,
)

EXECUTOR_WORKSPACE_ROOT = "/workspace"
SYSTEM_WORDLIST_ROOTS = tuple(
    Path(path).resolve(strict=False)
    for path in (
        "/usr/share/wordlists",
        "/usr/share/seclists",
        "/usr/share/skipfish/dictionaries",
    )
)


@dataclass(frozen=True)
class ResolvedWorkspacePath:
    """Resolved workspace path with both executor and artifact references."""

    host_path: Path
    execution_path: str
    artifact_ref: str


def effective_workspace_root() -> Path:
    """Return the task workspace, falling back to the current directory in tests."""

    try:
        return workspace_root()
    except OSError:
        fallback = Path(os.getenv("WORKSPACE") or Path.cwd()).resolve()
        fallback.mkdir(parents=True, exist_ok=True)
        (fallback / "artifacts").mkdir(parents=True, exist_ok=True)
        return fallback


def is_allowed_system_wordlist(path: str) -> bool:
    """Return whether an absolute path is under an approved system wordlist root."""

    normalized = str(path or "").strip().replace("\\", "/")
    if not normalized:
        return False

    candidate = Path(normalized)
    if not candidate.is_absolute():
        return False

    resolved_candidate = candidate.resolve(strict=False)
    return any(
        resolved_candidate == root or resolved_candidate.is_relative_to(root)
        for root in SYSTEM_WORDLIST_ROOTS
    )


def resolve_wordlist_path_for_execution(path: str) -> str:
    """Resolve a wordlist path for executor use.

    Known Kali system wordlist directories are allowed as absolute paths.
    All other paths must be workspace-relative.
    """

    normalized = str(path or "").strip()
    if not normalized:
        raise ValueError("wordlist path must not be empty")
    if is_allowed_system_wordlist(normalized):
        return str(Path(normalized).resolve(strict=False))
    if Path(normalized).is_absolute():
        raise ValueError(
            "Absolute wordlist paths are only allowed under /usr/share/wordlists, "
            "/usr/share/seclists, or /usr/share/skipfish/dictionaries"
        )
    return resolve_workspace_path_for_execution(normalized)


def resolve_workspace_path_for_execution(path: str) -> str:
    """Resolve a workspace-relative path to the executor-visible workspace path."""

    resolved = resolve_workspace_path(path)
    return resolved.execution_path


def resolve_workspace_path(path: str, *, create_parent: bool = False) -> ResolvedWorkspacePath:
    """Resolve a workspace-relative file or directory path without allowing escapes."""

    normalized = str(path or "").strip()
    if not normalized:
        raise ValueError("path must not be empty")
    if Path(normalized).is_absolute():
        raise ValueError("Absolute paths are not allowed; use workspace-relative paths.")

    root = effective_workspace_root()
    host_path = resolve_workspace_path_safe(normalized, workspace=root)
    if create_parent:
        host_path.parent.mkdir(parents=True, exist_ok=True)
    execution_path = resolve_container_path(
        str(host_path),
        host_workspace=str(root),
        container_workspace=EXECUTOR_WORKSPACE_ROOT,
    )
    return ResolvedWorkspacePath(
        host_path=host_path,
        execution_path=execution_path,
        artifact_ref=to_workspace_relative(host_path, root),
    )


def prepare_output_path_for_execution(path: str) -> str:
    """Resolve a workspace output file path and create its parent directory."""

    return resolve_workspace_path(path, create_parent=True).execution_path


def prepare_output_dir_for_execution(path: str) -> ResolvedWorkspacePath:
    """Resolve a workspace output directory and create only its parent."""

    return resolve_workspace_path(path, create_parent=True)


def touch_workspace_file_for_execution(path: str) -> ResolvedWorkspacePath:
    """Create an empty workspace file if missing and return executor/path refs."""

    resolved = resolve_workspace_path(path, create_parent=True)
    resolved.host_path.touch(exist_ok=True)
    return resolved
