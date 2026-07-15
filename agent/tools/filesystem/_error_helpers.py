"""LLM-friendly error message builders for filesystem tools.

This module provides helpers that transform generic error messages into
actionable guidance that enables LLM agents to self-correct without human
intervention.

Design Principles:
    1. Every error should include what went wrong
    2. Every error should suggest available alternatives
    3. Every error should recommend a next action
    4. Similar filename suggestions use fuzzy matching for typo recovery
"""

from __future__ import annotations

import difflib
import logging
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Maximum number of items to list in suggestions
MAX_AVAILABLE_FILES = 10
MAX_SIMILAR_SUGGESTIONS = 3
FUZZY_MATCH_THRESHOLD = 0.6


def build_llm_error(
    error_type: str,
    path: str,
    workspace: Path,
    message: str,
    *,
    context: Optional[dict] = None,
) -> str:
    """Build an LLM-friendly error message with recovery suggestions.

    This function transforms basic error messages into rich, actionable
    guidance that helps LLM agents recover from errors autonomously.

    Args:
        error_type: Category of error (not_found, is_directory, permission_denied,
                   path_out_of_workspace, missing_parent, would_overwrite, io_error)
        path: The path that caused the error (user-supplied, relative)
        workspace: The workspace root Path for context discovery
        message: The primary error message
        context: Optional additional context (e.g., {"existing_size": 1234})

    Returns:
        Multi-line error message with suggestions and next actions

    Example:
        >>> error = build_llm_error(
        ...     "not_found",
        ...     "redme.txt",
        ...     Path("/workspace/task_123"),
        ...     "File 'redme.txt' does not exist."
        ... )
        >>> print(error)
        File 'redme.txt' does not exist.
        Available files: README.md, config.yaml, results/
        Did you mean: README.md?
        Suggestion: Use filesystem.list_dir to browse the workspace.
    """
    context = context or {}
    parts = [message]

    if error_type == "not_found":
        parts.extend(_build_not_found_suggestions(path, workspace))

    elif error_type == "is_directory":
        parts.append(f"'{path}' is a directory, not a file.")
        parts.append("Use filesystem.list_dir to see its contents.")
        parts.append("Use filesystem.read_file with a specific file path inside the directory.")

    elif error_type == "permission_denied":
        parts.append("The file or directory cannot be accessed due to permissions.")
        parts.append("Possible causes:")
        parts.append("  - File is owned by another user/process")
        parts.append("  - File is currently locked by another application")
        parts.append("  - Insufficient privileges for this operation")
        parts.append("Suggestion: Check file permissions with filesystem.stat_path")

    elif error_type == "path_out_of_workspace":
        parts.append("All paths must be relative to the workspace root.")
        parts.append("Do not use:")
        parts.append("  - Absolute paths (e.g., /etc/passwd, C:\\Windows)")
        parts.append("  - Parent directory escapes (e.g., ../../../)")
        parts.append("Suggestion: Use filesystem.list_dir to see available workspace contents.")

    elif error_type == "missing_parent":
        parent_path = str(Path(path).parent) if Path(path).parent != Path(".") else "(root)"
        parts.append(f"The parent directory '{parent_path}' does not exist.")
        parts.append("Options:")
        parts.append("  1. Set create_parents=true to auto-create parent directories")
        parts.append("  2. Use filesystem.make_dir to create the directory first")
        parts.append("  3. Check if you have the correct path")
        # List available directories
        available_dirs = _list_available_directories(workspace)
        if available_dirs:
            parts.append(f"Existing directories: {', '.join(available_dirs[:5])}")

    elif error_type == "would_overwrite":
        parts.append("The file already exists and has different content.")
        parts.append("Safe mode prevents accidental overwrites.")
        parts.append("Options:")
        parts.append("  1. Set overwrite='overwrite' to replace the existing file")
        parts.append("  2. Use filesystem.read_file to inspect current content first")
        parts.append("  3. Use filesystem.append_file to add content instead of replacing")
        if "existing_size" in context:
            parts.append(f"Existing file size: {context['existing_size']} bytes")

    elif error_type == "io_error":
        parts.append("A system I/O error occurred while accessing the file.")
        parts.append("Possible causes:")
        parts.append("  - Disk is full")
        parts.append("  - File is corrupted")
        parts.append("  - Network filesystem unavailable")
        parts.append("Suggestion: Verify workspace state with filesystem.list_dir")

    elif error_type == "invalid_range":
        parts.append("The specified line range is invalid.")
        if "total_lines" in context:
            parts.append(f"File has {context['total_lines']} lines.")
        parts.append("Ensure start_line >= 1 and end_line >= start_line.")
        parts.append("Suggestion: Use filesystem.read_file with read_mode='head' to preview the file.")

    elif error_type == "line_out_of_range":
        total = context.get("total_lines", "unknown")
        parts.append(f"The file only has {total} lines.")
        parts.append("Options:")
        parts.append(f"  1. Use start_line={total} to append at end")
        parts.append("  2. Use filesystem.read_file to see actual file content")
        parts.append("  3. Use filesystem.append_file to add content at the end")

    elif error_type == "read_error":
        parts.append("Failed to read the file content.")
        parts.append("Possible causes:")
        parts.append("  - File encoding mismatch (try encoding='latin-1' or encoding=None for binary)")
        parts.append("  - File is being written by another process")
        parts.append("Suggestion: Use filesystem.stat_path to check file metadata.")

    elif error_type == "write_error":
        parts.append("Failed to write to the file.")
        parts.append("Possible causes:")
        parts.append("  - Disk space exhausted")
        parts.append("  - File is read-only")
        parts.append("  - Path is a directory")
        parts.append("Suggestion: Check available space and permissions.")

    elif error_type == "not_empty":
        parts.append("The directory is not empty and cannot be deleted.")
        parts.append("Options:")
        parts.append("  1. Set recursive=true to delete directory and all contents")
        parts.append("  2. Use filesystem.list_dir to see contents first")
        parts.append("  3. Delete individual files first")

    elif error_type == "already_exists":
        parts.append(f"'{path}' already exists.")
        parts.append("Options:")
        parts.append("  1. Choose a different name")
        parts.append("  2. Delete the existing item first with filesystem.delete_path")
        parts.append("  3. For directories, existing empty dirs are usually fine")

    elif error_type == "source_not_found":
        parts.extend(_build_not_found_suggestions(path, workspace, prefix="Source file"))

    elif error_type == "dest_exists":
        parts.append(f"Destination '{context.get('dest', path)}' already exists.")
        parts.append("Options:")
        parts.append("  1. Set overwrite=true to replace the destination")
        parts.append("  2. Choose a different destination path")
        parts.append("  3. Delete the destination first")

    else:
        # Generic fallback
        parts.append("Suggestion: Use filesystem.list_dir to explore the workspace.")
        logger.warning(f"Unknown error type in build_llm_error: {error_type}")

    return "\n".join(parts)


def _build_not_found_suggestions(
    path: str,
    workspace: Path,
    prefix: str = "File",
) -> List[str]:
    """Build suggestions for file-not-found errors."""
    suggestions = []

    # Get the directory we should look in
    target_path = Path(path)
    search_dir = workspace / target_path.parent if target_path.parent != Path(".") else workspace
    target_name = target_path.name

    # List available items
    available = _list_available_items(search_dir, max_items=MAX_AVAILABLE_FILES)
    if available:
        suggestions.append(f"Available in '{target_path.parent or '.'}': {', '.join(available)}")

    # Suggest similar names
    similar = _find_similar_names(target_name, available)
    if similar:
        best_match = similar[0]
        if target_path.parent != Path("."):
            best_match = str(target_path.parent / best_match)
        suggestions.append(f"Did you mean: {best_match}?")

    suggestions.append("Suggestion: Use filesystem.list_dir to browse the workspace.")

    return suggestions


def _list_available_items(
    directory: Path,
    max_items: int = MAX_AVAILABLE_FILES,
) -> List[str]:
    """List files and directories in a directory.

    Returns items formatted as:
    - 'filename' for files
    - 'dirname/' for directories (trailing slash indicates directory)
    """
    try:
        if not directory.exists() or not directory.is_dir():
            return []

        items = []
        for item in sorted(directory.iterdir()):
            if item.name.startswith("."):
                continue  # Skip hidden files
            if len(items) >= max_items:
                break
            name = f"{item.name}/" if item.is_dir() else item.name
            items.append(name)
        return items
    except (OSError, PermissionError):
        return []


def _list_available_directories(
    workspace: Path,
    max_items: int = 5,
) -> List[str]:
    """List only directories in workspace."""
    try:
        if not workspace.exists():
            return []
        dirs = []
        for item in sorted(workspace.iterdir()):
            if item.is_dir() and not item.name.startswith("."):
                dirs.append(item.name + "/")
                if len(dirs) >= max_items:
                    break
        return dirs
    except (OSError, PermissionError):
        return []


def _find_similar_names(
    target: str,
    candidates: List[str],
    threshold: float = FUZZY_MATCH_THRESHOLD,
    max_results: int = MAX_SIMILAR_SUGGESTIONS,
) -> List[str]:
    """Find similar filenames using difflib fuzzy matching.

    Handles:
    - Case differences (README.md vs readme.md)
    - Typos (confg.yaml vs config.yaml)
    - Missing extensions (config vs config.yaml)

    Args:
        target: The filename to match against
        candidates: List of available filenames
        threshold: Minimum similarity ratio (0.0-1.0)
        max_results: Maximum number of suggestions to return

    Returns:
        List of similar filenames, best match first
    """
    if not candidates:
        return []

    # Normalize for comparison (strip trailing slash from directories)
    target_lower = target.lower()
    normalized_candidates = []
    for c in candidates:
        clean = c.rstrip("/")
        normalized_candidates.append((clean.lower(), c))

    # Use difflib to find close matches
    candidate_names = [nc[0] for nc in normalized_candidates]
    matches = difflib.get_close_matches(
        target_lower,
        candidate_names,
        n=max_results,
        cutoff=threshold,
    )

    # Map back to original names
    result = []
    for match in matches:
        for normalized, original in normalized_candidates:
            if normalized == match:
                result.append(original.rstrip("/"))
                break

    return result


def format_error_with_path_context(
    error_type: str,
    path: str,
    workspace: Path,
    operation: str,
    details: Optional[str] = None,
) -> str:
    """Format an error with operation context for better LLM understanding.

    This is a convenience wrapper that adds operation context to errors.

    Args:
        error_type: The error category
        path: The problematic path
        workspace: Workspace root
        operation: The operation that failed (read, write, delete, etc.)
        details: Optional additional error details

    Returns:
        Formatted error message
    """
    base_message = f"Failed to {operation} '{path}'"
    if details:
        base_message = f"{base_message}: {details}"
    else:
        base_message = f"{base_message}."

    return build_llm_error(error_type, path, workspace, base_message)
