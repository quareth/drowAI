"""Registry for enhanced tool metadata definitions.

The registry provides functions to register and retrieve tool metadata.
Individual tools register their own metadata when imported, keeping
metadata co-located with implementation.
"""

from __future__ import annotations

from typing import Dict, Optional

from .categories import PentestPhase, ToolCategory
from .enhanced_metadata import EnhancedToolMetadata, ToolCapability, ToolCatalogRole

# Central registry mapping tool identifiers to their metadata
ENHANCED_TOOL_METADATA_REGISTRY: Dict[str, EnhancedToolMetadata] = {}


def register_enhanced_tool_metadata(metadata: EnhancedToolMetadata) -> None:
    """Register enhanced metadata for a tool.

    Parameters
    ----------
    metadata:
        The metadata object describing the tool.
    """
    try:
        from agent.tool_runtime.backend_tool_policy import resolve_execution_lane

        if resolve_execution_lane(metadata.tool_id) == "container_scoped":
            transports = metadata.supported_transports
            if transports is None:
                metadata.supported_transports = ["file-comm", "pty"]
            else:
                metadata.supported_transports = [
                    transport for transport in transports if transport != "direct"
                ]
    except Exception:
        pass
    ENHANCED_TOOL_METADATA_REGISTRY[metadata.tool_id] = metadata


def get_enhanced_tool_metadata(tool_id: str) -> Optional[EnhancedToolMetadata]:
    """Retrieve enhanced metadata for a given tool identifier.

    Notes
    -----
    Enhanced metadata is registered by tool modules at import time. Since the tool
    registry scans files without importing them, the metadata registry may be empty
    until relevant modules are loaded.

    To keep selection and tests reliable, this function attempts a best-effort lazy
    import of the tool module on cache miss.
    """
    existing = ENHANCED_TOOL_METADATA_REGISTRY.get(tool_id)
    if existing is not None:
        return existing

    # Best-effort lazy load: importing the tool module will register its metadata.
    try:
        from .tool_registry import get_tool

        # get_tool triggers module import and tool class discovery/registration.
        get_tool(tool_id)
    except Exception:
        return None

    return ENHANCED_TOOL_METADATA_REGISTRY.get(tool_id)


def get_all_enhanced_metadata() -> Dict[str, EnhancedToolMetadata]:
    """Return a shallow copy of all registered enhanced tool metadata."""
    return ENHANCED_TOOL_METADATA_REGISTRY.copy()


# Re-export for convenience
__all__ = [
    "register_enhanced_tool_metadata",
    "get_enhanced_tool_metadata",
    "get_all_enhanced_metadata",
    "EnhancedToolMetadata",
    "ToolCapability",
    "ToolCatalogRole",
    "ToolCategory",
    "PentestPhase",
]
