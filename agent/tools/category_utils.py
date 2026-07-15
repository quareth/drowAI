"""Utilities for extracting and working with tool categories."""

from __future__ import annotations

import logging
from typing import Dict, List, Set

from core.tool_category_taxonomy import (
    get_category_descriptions as get_canonical_category_descriptions,
)

logger = logging.getLogger(__name__)

_CATEGORY_TOOL_EXPOSURE_OVERLAY = {
    "web_applications": (
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ),
}


def get_tool_categories() -> List[str]:
    """Extract unique visible tool categories from the registry.

    Tool IDs follow the pattern: category.subcategory.tool_name
    Examples:
        - information_gathering.network_discovery.nmap -> "information_gathering"
        - exploitation_tools.metasploit.run_exploit -> "exploitation_tools"
        - database_assessment.database_enumeration.sqlmap -> "database_assessment"

    Returns:
        Sorted list of unique top-level categories
    """
    try:
        from .catalog_visibility import visible_available_tools
        from .enhanced_metadata_registry import get_enhanced_tool_metadata
    except ImportError:
        logger.error("[CATEGORIES] Failed to import catalog visibility policy")
        return []

    all_tools = visible_available_tools()
    categories: Set[str] = set()

    for tool_id in all_tools:
        # Extract category (first part before first dot)
        if "." in tool_id:
            category = tool_id.split(".")[0]
            categories.add(category)
            continue

        metadata = get_enhanced_tool_metadata(tool_id)
        metadata_category = getattr(getattr(metadata, "category", None), "value", None)
        if isinstance(metadata_category, str) and metadata_category:
            categories.add(metadata_category)

    sorted_categories = sorted(categories)

    logger.info(
        f"[CATEGORIES] Extracted {len(sorted_categories)} categories from {len(all_tools)} visible tools: "
        f"{sorted_categories}"
    )

    return sorted_categories


def get_tools_for_categories(categories: List[str]) -> List[str]:
    """Filter visible tools to only those in specified categories, sorted by priority.

    Args:
        categories: List of category names (e.g., ["information_gathering", "exploitation_tools"])

    Returns:
        List of tool IDs that belong to any of the specified categories,
        sorted by execution_priority (high to low) then alphabetically
    """
    if not categories:
        logger.warning("[CATEGORIES] No categories specified, returning empty list")
        return []

    try:
        from .catalog_visibility import visible_available_tools
        from .enhanced_metadata_registry import get_enhanced_tool_metadata
    except ImportError:
        logger.error("[CATEGORIES] Failed to import catalog visibility or metadata registry")
        return []

    all_tools = visible_available_tools()
    filtered_tools: List[str] = []

    # Normalize categories to lowercase for comparison
    normalized_categories = {cat.lower() for cat in categories}

    for tool_id in all_tools:
        if "." in tool_id:
            tool_category = tool_id.split(".")[0].lower()
            if tool_category in normalized_categories:
                filtered_tools.append(tool_id)
            continue

        metadata = get_enhanced_tool_metadata(tool_id)
        metadata_category = getattr(getattr(metadata, "category", None), "value", None)
        if (
            isinstance(metadata_category, str)
            and metadata_category.lower() in normalized_categories
        ):
            filtered_tools.append(tool_id)
    for category in normalized_categories:
        for tool_id in _CATEGORY_TOOL_EXPOSURE_OVERLAY.get(category, ()):
            if tool_id in all_tools and tool_id not in filtered_tools:
                filtered_tools.append(tool_id)

    # Sort by priority: execution_priority (high to low), then alphabetically
    def get_sort_key(tool_id: str) -> tuple:
        """Get sort key: (negative_priority, tool_id) for high-priority-first sorting."""
        metadata = get_enhanced_tool_metadata(tool_id)
        priority = metadata.execution_priority if metadata else 5  # Default priority
        return (-priority, tool_id)  # Negative for descending priority

    sorted_tools = sorted(filtered_tools, key=get_sort_key)

    logger.info(
        f"[CATEGORIES] Filtered to {len(sorted_tools)} tools from categories: {categories} "
        f"(out of {len(all_tools)} visible tools), sorted by priority"
    )
    logger.debug(f"[CATEGORIES] Top 10 tools: {sorted_tools[:10]}")

    return sorted_tools


def get_category_descriptions() -> Dict[str, str]:
    """Get human-readable descriptions for each category.

    Returns:
        Dict mapping category name to description
    """
    return get_canonical_category_descriptions()


__all__ = [
    "get_tool_categories",
    "get_tools_for_categories",
    "get_category_descriptions",
]
