"""Shared builder for assembling visible tool ID lists from the tool registry.

Provides the complete visible tool catalog for LLM-based selection flows.
Consumed by both the enhanced planner and the tool-execution runtime planner
service.
"""

from __future__ import annotations

import logging
from typing import List


def build_full_tool_catalog(*, logger: logging.Logger) -> List[str]:
    """Build the complete visible tool catalog without capability filtering."""
    try:
        from agent.tools.catalog_visibility import visible_available_tools
    except ImportError as exc:
        logger.warning("[PLANNER_CATALOG] Could not import catalog visibility: %s", exc)
        return []

    all_tools = visible_available_tools()
    logger.debug(
        "[PLANNER_CATALOG] visible_available_tools() returned %d items",
        len(all_tools),
    )
    if all_tools:
        logger.debug("[PLANNER_CATALOG] First few tools: %s", all_tools[:5])
    if not all_tools:
        logger.warning("[PLANNER_CATALOG] Tool registry is empty")
        return []

    valid_tools = [t for t in all_tools if "." in str(t) or "_" in str(t)]
    if not valid_tools:
        logger.warning(
            "[PLANNER_CATALOG] No valid tool IDs found in registry. "
            "Got: %s. These look like metadata keys, not tool IDs!",
            all_tools[:10],
        )
        valid_tools = all_tools

    logger.info(
        "[PLANNER_CATALOG] Providing all %d visible tools to planner "
        "(from %d available)",
        len(valid_tools),
        len(all_tools),
    )
    logger.debug("[PLANNER_CATALOG] Catalog tools: %s", valid_tools)
    return valid_tools
