"""Catalog role policy for user-facing tool configuration surfaces.

This module classifies executable tools as pentest, utility, or system tools.
It is the single place future UI/API layers should ask whether a tool is
user-configurable instead of duplicating frontend exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .categories import ToolCategory
from .enhanced_metadata import EnhancedToolMetadata, ToolCatalogRole

_UTILITY_TOP_LEVELS: frozenset[str] = frozenset(
    {
        "filesystem",
        "networking_utilities",
        "reporting_tools",
        "service_access",
        "shell",
    }
)
_SYSTEM_TOP_LEVELS: frozenset[str] = frozenset({"artifact", "knowledge"})

_UTILITY_CATEGORIES: frozenset[ToolCategory] = frozenset(
    {
        ToolCategory.NETWORKING_UTILITIES,
        ToolCategory.REPORTING_TOOLS,
        ToolCategory.SERVICE_ACCESS,
        ToolCategory.SHELL,
        ToolCategory.WORKSPACE_FILESYSTEM,
    }
)
_SYSTEM_CATEGORIES: frozenset[ToolCategory] = frozenset({ToolCategory.KNOWLEDGE})


@dataclass(frozen=True, slots=True)
class ToolCatalogRoleResolution:
    """Resolved catalog role plus provenance for inspection/reporting."""

    catalog_role: ToolCatalogRole
    role_source: str


def _normalize_tool_id(tool_id: Any) -> str:
    """Return a stripped string tool id, or an empty string for missing input."""

    return str(tool_id or "").strip()


def _top_level(tool_id: str) -> str:
    """Return the top-level namespace for a dotted tool id."""

    return tool_id.split(".", 1)[0] if "." in tool_id else tool_id


def _metadata_declares_catalog_role(metadata: EnhancedToolMetadata) -> bool:
    """Return whether catalog_role was explicitly supplied on the metadata."""

    fields_set = getattr(metadata, "model_fields_set", set())
    return "catalog_role" in fields_set


def _fallback_role(
    tool_id: str,
    metadata: Optional[EnhancedToolMetadata],
) -> ToolCatalogRole:
    """Resolve role from category/top-level policy when metadata is not explicit."""

    category = metadata.category if metadata is not None else None
    if category in _SYSTEM_CATEGORIES or _top_level(tool_id) in _SYSTEM_TOP_LEVELS:
        return ToolCatalogRole.SYSTEM
    if category in _UTILITY_CATEGORIES or _top_level(tool_id) in _UTILITY_TOP_LEVELS:
        return ToolCatalogRole.UTILITY
    return ToolCatalogRole.PENTEST


def resolve_tool_catalog_role(tool_id: Any) -> ToolCatalogRoleResolution:
    """Return the catalog role for any executable tool id."""

    normalized = _normalize_tool_id(tool_id)
    metadata: Optional[EnhancedToolMetadata] = None
    if normalized:
        try:
            from .enhanced_metadata_registry import get_enhanced_tool_metadata

            metadata = get_enhanced_tool_metadata(normalized)
        except Exception:
            metadata = None

    if metadata is not None and _metadata_declares_catalog_role(metadata):
        return ToolCatalogRoleResolution(
            catalog_role=metadata.catalog_role,
            role_source="metadata",
        )

    return ToolCatalogRoleResolution(
        catalog_role=_fallback_role(normalized, metadata),
        role_source="fallback",
    )


def get_tool_catalog_role(tool_id: Any) -> ToolCatalogRole:
    """Return the resolved catalog role for a tool id."""

    return resolve_tool_catalog_role(tool_id).catalog_role


def is_user_configurable_tool(tool_id: Any) -> bool:
    """Return whether user-facing catalog/runbook configuration may target a tool."""

    return get_tool_catalog_role(tool_id) == ToolCatalogRole.PENTEST


__all__ = [
    "ToolCatalogRoleResolution",
    "get_tool_catalog_role",
    "is_user_configurable_tool",
    "resolve_tool_catalog_role",
]
