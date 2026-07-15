"""Deterministic adapter dispatch registry for semantic extraction.

This module owns adapter matching by:
1) source tool name
2) capability-family fallback"""

from __future__ import annotations

from typing import Iterable

from .base import AdapterContext, KnowledgeAdapter


class AdapterRegistry:
    """In-memory deterministic adapter resolver."""

    def __init__(self, adapters: Iterable[KnowledgeAdapter] | None = None) -> None:
        self._adapters: list[KnowledgeAdapter] = list(adapters or [])

    def register(self, adapter: KnowledgeAdapter) -> None:
        """Register one adapter implementation preserving insertion order."""
        self._adapters.append(adapter)

    def resolve(self, context: AdapterContext) -> list[KnowledgeAdapter]:
        """Resolve matching adapters using tool-name first, capability fallback second."""
        source_tool_name = context.source_tool_name()
        capability_family = context.capability_family()

        tool_matches = [
            adapter
            for adapter in self._adapters
            if source_tool_name and source_tool_name in tuple(adapter.tool_names or ())
        ]
        if tool_matches:
            return [adapter for adapter in tool_matches if adapter.supports(context)]

        capability_matches = []
        for adapter in self._adapters:
            if not capability_family:
                continue
            if capability_family not in tuple(adapter.capability_families or ()):
                continue
            adapter_tool_names = tuple(adapter.tool_names or ())
            # Avoid cross-tool false positives for concrete adapters bound to
            # explicit tool ids. Capability fallback is reserved for generic
            # adapters that intentionally declare no tool-name binding.
            if source_tool_name and adapter_tool_names:
                continue
            capability_matches.append(adapter)
        return [adapter for adapter in capability_matches if adapter.supports(context)]
