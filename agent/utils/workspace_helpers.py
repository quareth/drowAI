"""Workspace path utilities for artifact management and indexing.

Extracted from automatic mode's advanced_context_manager and enhanced
for production use across all execution modes.

This module provides centralized, production-ready workspace path resolution
that eliminates fragile relative path dependencies and ensures consistent
behavior between automatic mode and LangGraph.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional
import posixpath


@contextmanager
def temporary_cwd(target_dir: str):
    """Temporarily change process working directory."""
    prev = os.getcwd()
    try:
        os.chdir(target_dir)
        yield
    finally:
        try:
            os.chdir(prev)
        except Exception:
            pass


def resolve_host_workspace_path(
    task_id: Any,
    workspace_hint: Optional[str] = None,
) -> str:
    """Resolve the host workspace path for a task.

    Resolution order:
    1) explicit workspace_hint if it exists on disk
    2) fail closed when runtime metadata is missing or invalid
    """
    try:
        if workspace_hint:
            candidate = Path(workspace_hint)
            if candidate.exists() and candidate.is_dir():
                return str(candidate)
    except Exception:
        pass

    raise ValueError(
        "Host workspace path requires provider/runtime metadata; "
        "workspace_hint was not provided or did not resolve to an existing directory"
    )


def resolve_container_path(path: str, host_workspace: Optional[str] = None, *, container_workspace: str = "/workspace") -> str:
    """Resolve a host or relative path to a container workspace path.

    Relative paths are joined under `container_workspace`.
    Host paths under `host_workspace` are translated to container mount path.
    Container-native absolute paths under the mounted workspace are allowed.
    """
    if not path or path == ".":
        return container_workspace

    if os.path.isabs(path):
        host_workspace_norm = (
            posixpath.normpath(str(host_workspace).replace("\\", "/")) if host_workspace else ""
        )
        path_norm = posixpath.normpath(str(path).replace("\\", "/"))

        if host_workspace_norm:
            if (
                path_norm == host_workspace_norm
                or path_norm.startswith(f"{host_workspace_norm}/")
            ):
                relative_part = os.path.relpath(path_norm, host_workspace_norm)
                relative_part = relative_part.replace("\\", "/").lstrip("/")
                if relative_part in ("", "."):
                    return container_workspace
                return f"{container_workspace}/{relative_part}"

        if path_norm == container_workspace or path_norm.startswith(f"{container_workspace}/"):
            return path_norm
        raise ValueError(f"Path {path} cannot be resolved for container")

    relative_posix = str(path).replace("\\", "/")
    normalized = posixpath.normpath(posixpath.join(container_workspace, relative_posix))

    if normalized != container_workspace and not normalized.startswith(f"{container_workspace}/"):
        raise ValueError(f"Path {path} resolves outside container workspace")

    return normalized


def resolve_workspace_path_for_executor(path: str, *, workspace_path: str = "/workspace") -> str:
    """Resolve and validate a path against a configured workspace.

    Relative paths are converted to workspace-relative absolute paths.
    Absolute paths must stay within workspace boundaries.
    """
    workspace_path_normalized = posixpath.normpath(str(workspace_path).replace("\\", "/"))

    # Absolute path: validate it's inside workspace.
    if os.path.isabs(path):
        path_normalized = posixpath.normpath(str(path).replace("\\", "/"))
        if (
            path_normalized != workspace_path_normalized
            and not path_normalized.startswith(f"{workspace_path_normalized}/")
        ):
            raise ValueError(f"Path {path} is outside workspace {workspace_path}")
        return path_normalized

    # Relative path: resolve under workspace.
    full_path = posixpath.normpath(
        posixpath.join(workspace_path_normalized, str(path).replace("\\", "/"))
    )

    if (
        full_path != workspace_path_normalized
        and not full_path.startswith(f"{workspace_path_normalized}/")
    ):
        raise ValueError(f"Path {path} resolves outside workspace {workspace_path}")

    return full_path


def get_index_directory(
    workspace_path: str,
    respect_env_override: bool = True
) -> str:
    """
    Get index directory following DrowAI workspace conventions.
    
    Resolution order:
    1. Environment variable CONTEXT_INDEX_DIR (if respect_env_override=True)
    2. workspace/context/../index (if context dir exists)
    3. workspace/index (default fallback)
    
    This matches the automatic mode's resolution logic, ensuring consistent
    index location across execution modes.
    
    Args:
        workspace_path: Path to task workspace (e.g., /workspace/1423)
        respect_env_override: Whether to check CONTEXT_INDEX_DIR env var
    
    Returns:
        Absolute path to index directory
    
    Example:
        >>> # Default behavior: workspace/index
        >>> get_index_directory("/workspace/1423")
        '/workspace/1423/index'
        
        >>> # With env override
        >>> os.environ["CONTEXT_INDEX_DIR"] = "/custom/index"
        >>> get_index_directory("/workspace/1423")
        '/custom/index'
        
        >>> # Legacy context dir pattern
        >>> # If /workspace/1423/context exists, returns /workspace/1423/index
    """
    # Check env var override first (if enabled)
    if respect_env_override:
        env_index = os.getenv("CONTEXT_INDEX_DIR")
        if env_index:
            return env_index
    
    workspace = Path(workspace_path)
    
    # Try context-relative pattern (legacy compatibility)
    # This matches automatic mode's: context_dir.parent / "index"
    context_dir = workspace / "context"
    if context_dir.exists():
        return str(context_dir.parent / "index")
    
    # Default: workspace/index
    return str(workspace / "index")


def get_run_id_from_workspace(workspace_path: str) -> str:
    """
    Extract run ID from workspace path.
    
    For workspace path like /workspace/1423, returns "1423".
    For workspace path like /workspace/task-1423, returns "task-1423".
    
    This matches automatic mode's convention of using workspace folder name
    as the run identifier for artifact indexing.
    
    Args:
        workspace_path: Path to task workspace
    
    Returns:
        Run ID (workspace folder name) or "default" if extraction fails
    
    Example:
        >>> get_run_id_from_workspace("/workspace/1423")
        '1423'
        
        >>> get_run_id_from_workspace("/workspace/task-1423")
        'task-1423'
        
        >>> get_run_id_from_workspace("")
        'default'
    """
    try:
        folder_name = Path(workspace_path).name
        if folder_name and folder_name != ".":
            return folder_name
        return "default"
    except Exception:
        return "default"


def ensure_workspace_directories(workspace_path: str) -> None:
    """
    Ensure required workspace subdirectories exist.
    
    Creates:
    - workspace/artifacts/ (for tool outputs)
    - workspace/index/ (for semantic chunks)
    
    This function is idempotent and safe to call multiple times.
    It will not raise exceptions if directories already exist.
    
    Args:
        workspace_path: Path to task workspace
    
    Raises:
        OSError: If directory creation fails due to permissions or disk space
    
    Example:
        >>> ensure_workspace_directories("/workspace/1423")
        # Creates:
        # /workspace/1423/artifacts/
        # /workspace/1423/index/
    """
    workspace = Path(workspace_path)
    
    # Create artifacts directory
    artifacts_dir = workspace / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    
    # Create index directory
    index_dir = workspace / "index"
    index_dir.mkdir(parents=True, exist_ok=True)


def resolve_workspace_path(
    workspace_override: Optional[str] = None,
    fallback_to_env: bool = True
) -> str:
    """
    Resolve workspace path from override, environment, or default.
    
    Resolution order:
    1. workspace_override parameter (if provided)
    2. WORKSPACE environment variable (if fallback_to_env=True)
    3. "/workspace" (default fallback)
    
    Args:
        workspace_override: Optional explicit workspace path
        fallback_to_env: Whether to check WORKSPACE env var
    
    Returns:
        Resolved workspace path
    
    Example:
        >>> resolve_workspace_path(workspace_override="/custom/workspace")
        '/custom/workspace'
        
        >>> os.environ["WORKSPACE"] = "/workspace/1423"
        >>> resolve_workspace_path()
        '/workspace/1423'
        
        >>> resolve_workspace_path(fallback_to_env=False)
        '/workspace'
    """
    # Use explicit override if provided
    if workspace_override:
        return workspace_override
    
    # Check environment variable
    if fallback_to_env:
        env_workspace = os.getenv("WORKSPACE")
        if env_workspace:
            return env_workspace
    
    # Default fallback
    return "/workspace"


__all__ = [
    "get_index_directory",
    "get_run_id_from_workspace",
    "ensure_workspace_directories",
    "resolve_workspace_path",
    "temporary_cwd",
    "resolve_host_workspace_path",
    "resolve_container_path",
    "resolve_workspace_path_for_executor",
]

