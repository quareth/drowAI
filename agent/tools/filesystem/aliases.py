"""Deprecation aliases for fs.* namespace.

The fs.* namespace is deprecated in favor of filesystem.* namespace.
These aliases provide backward compatibility while logging deprecation warnings.
"""

from __future__ import annotations

import logging
from typing import Dict

logger = logging.getLogger(__name__)

# Mapping from deprecated fs.* tool IDs to canonical filesystem.* tool IDs
FS_NAMESPACE_ALIASES: Dict[str, str] = {
    "fs.read_file": "filesystem.read_file",
    "fs.write_file": "filesystem.write_file",
    "fs.append_file": "filesystem.append_file",
    "fs.delete": "filesystem.delete_path",
    "fs.make_dir": "filesystem.make_dir",
    "fs.list_dir": "filesystem.list_dir",
    "fs.move": "filesystem.move_path",
    "fs.copy": "filesystem.copy_path",
    "fs.stat": "filesystem.stat_path",
    "fs.find": "filesystem.find_paths",
    "fs.search_text": "filesystem.search_text",
}


def resolve_tool_alias(tool_id: str) -> str:
    """Resolve fs.* aliases to filesystem.* with deprecation warning.
    
    Args:
        tool_id: Tool identifier that may use deprecated fs.* namespace
        
    Returns:
        Canonical tool_id using filesystem.* namespace
        
    Example:
        >>> resolve_tool_alias("fs.read_file")
        # Logs: "Tool 'fs.read_file' is deprecated..."
        "filesystem.read_file"
        
        >>> resolve_tool_alias("filesystem.read_file")
        "filesystem.read_file"
    """
    if tool_id in FS_NAMESPACE_ALIASES:
        new_id = FS_NAMESPACE_ALIASES[tool_id]
        logger.warning(
            f"Tool '{tool_id}' is deprecated. Use '{new_id}' instead. "
            f"The fs.* namespace will be removed in a future version."
        )
        return new_id
    return tool_id


def is_deprecated_fs_tool(tool_id: str) -> bool:
    """Check if a tool_id uses the deprecated fs.* namespace.
    
    Args:
        tool_id: Tool identifier to check
        
    Returns:
        True if tool_id is a deprecated fs.* tool
    """
    return tool_id in FS_NAMESPACE_ALIASES


def get_canonical_tool_id(tool_id: str) -> str:
    """Get canonical tool_id without logging deprecation warning.
    
    Use this when you need to normalize tool IDs without side effects.
    
    Args:
        tool_id: Tool identifier that may use deprecated fs.* namespace
        
    Returns:
        Canonical tool_id using filesystem.* namespace
    """
    return FS_NAMESPACE_ALIASES.get(tool_id, tool_id)
