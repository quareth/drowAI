"""Shared utilities for agent components.

This package contains reusable utilities that are shared between
automatic mode and LangGraph interactive mode.
"""

from agent.utils.artifact_manager import save_tool_output_artifact
from agent.utils.workspace_helpers import (
    get_index_directory,
    get_run_id_from_workspace,
    ensure_workspace_directories,
    resolve_workspace_path,
    temporary_cwd,
    resolve_host_workspace_path,
    resolve_container_path,
    resolve_workspace_path_for_executor,
)
from agent.utils.output_processing import (
    smart_truncate,
    extract_error_lines,
    strip_noise,
    process_tool_output,
    format_output_for_prompt,
    suggest_read_strategy,
    ProcessedOutput,
)

__all__ = [
    # Artifact management
    "save_tool_output_artifact",
    # Workspace helpers
    "get_index_directory",
    "get_run_id_from_workspace",
    "ensure_workspace_directories",
    "resolve_workspace_path",
    "temporary_cwd",
    "resolve_host_workspace_path",
    "resolve_container_path",
    "resolve_workspace_path_for_executor",
    # Output processing
    "smart_truncate",
    "extract_error_lines",
    "strip_noise",
    "process_tool_output",
    "format_output_for_prompt",
    "suggest_read_strategy",
    "ProcessedOutput",
]

