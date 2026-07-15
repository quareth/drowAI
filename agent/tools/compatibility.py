"""Utilities for analyzing tool execution compatibility.

This module provides a simple compatibility matrix that defines which tools
can execute concurrently and exposes helpers to group tools into compatible
execution batches.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Iterable, List, Tuple

from .enhanced_metadata_registry import (
    get_all_enhanced_metadata,
    get_enhanced_tool_metadata,
)
from .categories import ToolCategory


class CompatibilityLevel(Enum):
    """Represents how safely two tools can run together."""

    COMPATIBLE = "compatible"
    SEQUENTIAL = "sequential"
    EXCLUSIVE = "exclusive"


class ToolCompatibilityAnalyzer:
    """Analyze tool compatibility for concurrent execution."""

    def __init__(self) -> None:
        self.compatibility_matrix: Dict[Tuple[str, str], CompatibilityLevel]
        self.compatibility_matrix = self._build_compatibility_matrix()

    def can_run_together(self, tool1: str, tool2: str) -> bool:
        """Return ``True`` if ``tool1`` and ``tool2`` can run concurrently."""

        level = self.compatibility_matrix.get((tool1, tool2))
        if level is None:
            # Default to compatibility when no rule exists.
            return True
        return level is CompatibilityLevel.COMPATIBLE

    def group_compatible_tools(self, tools: Iterable[str]) -> List[List[str]]:
        """Group ``tools`` into sets that can run concurrently.

        The algorithm iteratively builds batches by checking that every new tool
        is compatible with the tools already in the current batch.
        """

        remaining = list(tools)
        groups: List[List[str]] = []
        while remaining:
            current = [remaining.pop(0)]
            i = 0
            while i < len(remaining):
                candidate = remaining[i]
                if all(
                    self.can_run_together(candidate, existing)
                    and self.can_run_together(existing, candidate)
                    for existing in current
                ):
                    current.append(remaining.pop(i))
                else:
                    i += 1
            groups.append(current)
        return groups

    def _build_compatibility_matrix(self) -> Dict[Tuple[str, str], CompatibilityLevel]:
        """Create the default compatibility matrix using enhanced metadata.

        Rules:
        - Tools in the same non-exploitation category default to COMPATIBLE
        - EXPLOITATION tools default to EXCLUSIVE with everything (including themselves)
        - Explicit exceptions can be layered if needed in future
        """

        matrix: Dict[Tuple[str, str], CompatibilityLevel] = {}

        # Enhanced metadata is registered by tool modules at import time. Warm
        # it explicitly so standalone compatibility checks don't silently see
        # only the modules imported earlier in the process.
        try:
            from .tool_registry import available_tools

            for tool_id in available_tools():
                get_enhanced_tool_metadata(tool_id)
        except Exception:
            pass

        metadata = get_all_enhanced_metadata()

        # Group tools by category
        by_category: Dict[ToolCategory, List[str]] = {}
        for tool_id, meta in metadata.items():
            by_category.setdefault(meta.category, []).append(tool_id)

        def register_pairs(tools: List[str], level: CompatibilityLevel) -> None:
            for t1 in tools:
                for t2 in tools:
                    if t1 == t2:
                        continue
                    matrix[(t1, t2)] = level

        # Non-exploitation categories are compatible within category
        compatible_categories = [
            ToolCategory.NETWORK_DISCOVERY,
            ToolCategory.DNS_ENUMERATION,
            ToolCategory.WEB_CRAWLING,
            ToolCategory.WEB_VULNERABILITY_SCANNING,
            ToolCategory.APPLICATION_PROXY,
            ToolCategory.WEB_ENUMERATION,
            ToolCategory.SYSTEM_SERVICES,
            ToolCategory.NETWORKING_UTILITIES,
        ]
        for cat in compatible_categories:
            tools = by_category.get(cat, [])
            register_pairs(tools, CompatibilityLevel.COMPATIBLE)

        # Exploitation (exclusive with everything, including themselves)
        exploitation_tools = by_category.get(ToolCategory.EXPLOITATION_TOOLS, [])
        for t1 in exploitation_tools:
            for t2 in metadata.keys():
                if t1 == t2:
                    continue
                matrix[(t1, t2)] = CompatibilityLevel.EXCLUSIVE
                matrix[(t2, t1)] = CompatibilityLevel.EXCLUSIVE

        # Additionally mark SQLMap as exclusive
        sqlmap_ids = [tid for tid in metadata.keys() if tid.endswith(".sqlmap")]
        for t1 in sqlmap_ids:
            for t2 in metadata.keys():
                if t1 == t2:
                    continue
                matrix[(t1, t2)] = CompatibilityLevel.EXCLUSIVE
                matrix[(t2, t1)] = CompatibilityLevel.EXCLUSIVE

        return matrix
