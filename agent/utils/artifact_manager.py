"""Shared utility for saving tool output artifacts to disk.

This module provides the filesystem write primitive for runtime workspace
artifacts. Higher-level runtime services decide when to call it and whether to
index the saved file.

Enhancements over original:
- Always saves stderr (not optional)
- Proper typing and documentation
- Consistent error handling
- Enforces task-workspace-only artifact storage (no /tmp fallback)
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional


def save_tool_output_artifact(
    workspace_path: str,
    stdout: str,
    stderr: str = "",
    logger: Optional[object] = None,
) -> str:
    """
    Save tool output to timestamped artifact file.
    
    The function creates an artifacts/ directory within the workspace,
    generates a timestamped filename, and writes both stdout and stderr
    to the file. If the workspace is not writable, the write fails and
    no artifact path is returned.
    
    Args:
        workspace_path: Path to task workspace (e.g., /workspace/1423)
        stdout: Tool stdout content
        stderr: Tool stderr content (optional)
        logger: Optional logger for debug/error messages
    
    Returns:
        Workspace-relative path to saved artifact file, or empty string on failure
    
    Example:
        >>> artifact_path = save_tool_output_artifact(
        ...     workspace_path="/workspace/1423",
        ...     stdout="Port 80 is open\\n",
        ...     stderr="",
        ...     logger=my_logger
        ... )
        >>> print(artifact_path)
        artifacts/20250129123456000000_tool.txt
    """
    # Create artifacts directory inside the task workspace.
    artifacts_dir = os.path.join(workspace_path, "artifacts")
    
    # Workspace-only policy: no fallback locations outside task workspace.
    try:
        os.makedirs(artifacts_dir, exist_ok=True)
    except (OSError, PermissionError) as e:
        if logger:
            logger.log_operation("ERROR", f"Workspace artifacts directory is not writable: {e}")
        return ""
    
    # Generate timestamp-based filename with microsecond precision to prevent collisions
    # Multiple tool executions within the same second would overwrite each other with second-only precision
    timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")  # Added %f for microseconds
    filename = f"{timestamp}_tool.txt"
    absolute_path = os.path.join(artifacts_dir, filename)
    
    try:
        with open(absolute_path, "w", encoding="utf-8") as f:
            # Write stdout (primary output)
            f.write(stdout)
            
            # Optionally append stderr if present (ENHANCEMENT: always included)
            if stderr:
                f.write("\n\n=== STDERR ===\n")
                f.write(stderr)
        
        if logger:
            logger.log_operation("DEBUG", f"Stored tool output to: {absolute_path}")
    
    except Exception as exc:  # pragma: no cover - best effort
        if logger:
            logger.log_operation("ERROR", f"Failed to write tool output artifact: {exc}")
        return ""
    
    # Return RELATIVE path so it works on both backend and Kali container
    # Backend workspace: /workspaces/drowAI/.../task-1557/artifacts/file.txt
    # Kali workspace: /workspace/artifacts/file.txt
    # Using "artifacts/file.txt" works on both when resolved relative to workspace root
    return f"artifacts/{filename}"


__all__ = ["save_tool_output_artifact"]
