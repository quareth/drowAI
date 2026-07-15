"""Workspace management, file browsing, and environment collection services."""

from .manager import WorkspaceManager, get_workspace_manager
from .file_browser_service import FileBrowserService
from .environment_collector import (
    ENV_INFO_FILENAME,
    collect_environment_info,
    format_environment_compact,
    format_environment_for_prompt,
    load_environment_info,
    save_environment_info,
)

__all__ = [
    "WorkspaceManager",
    "get_workspace_manager",
    "FileBrowserService",
    "ENV_INFO_FILENAME",
    "collect_environment_info",
    "format_environment_compact",
    "format_environment_for_prompt",
    "load_environment_info",
    "save_environment_info",
]
