"""Definition-parameterized subagent tool profile resolution.

Purpose
-------
Resolve the least-privilege tool profile for a declarative subagent definition
from the current LLM-visible tool catalog.

Responsibility boundary
-----------------------
This module validates static tool visibility and metadata only. It does not
execute tools, mutate graph state, authorize targets, depend on control-plane
services, or dispatch subagent runs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from agent.subagents.definition import (
    SubagentDefinition,
    resolve_definition_capability,
)
from agent.tools.catalog_policy import get_tool_catalog_role
from agent.tools.catalog_visibility import visible_available_tools
from agent.tools.categories import ToolCategory
from agent.tools.enhanced_metadata import EnhancedToolMetadata, ToolCatalogRole
from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata


_PLATFORM_FORBIDDEN_CATEGORIES: frozenset[ToolCategory] = frozenset(
    {
        ToolCategory.SHELL,
    }
)
_PLATFORM_FORBIDDEN_TOOL_PREFIXES: tuple[str, ...] = (
    "shell.",
)


@dataclass(frozen=True, slots=True)
class SubagentToolSpec:
    """A definition-visible tool with normalized owned capabilities."""

    tool_id: str
    display_name: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubagentToolProfile:
    """Resolved tool profile used by definition-configured runtime nodes."""

    tools: tuple[SubagentToolSpec, ...]
    definition_id: str = ""

    @property
    def tool_ids(self) -> tuple[str, ...]:
        """Return visible tool ids in deterministic catalog order."""

        return tuple(tool.tool_id for tool in self.tools)

    def capabilities_for_tool(self, tool_id: str) -> tuple[str, ...]:
        """Return normalized owned capabilities for a visible tool id."""

        normalized = _normalize_tool_id(tool_id)
        for tool in self.tools:
            if tool.tool_id == normalized:
                return tool.capabilities
        return ()


def resolve_subagent_tool_profile(
    definition: SubagentDefinition,
    visible_tool_ids: Iterable[Any] | None = None,
) -> SubagentToolProfile:
    """Resolve a bounded tool profile from definition tool ids and metadata."""

    candidate_ids = (
        visible_available_tools() if visible_tool_ids is None else visible_tool_ids
    )
    tools: list[SubagentToolSpec] = []
    seen: set[str] = set()
    for raw_tool_id in candidate_ids:
        tool_id = _normalize_tool_id(raw_tool_id)
        if not tool_id or tool_id in seen:
            continue
        seen.add(tool_id)

        metadata = get_enhanced_tool_metadata(tool_id)
        if metadata is None:
            continue

        capabilities = subagent_capabilities_from_metadata(
            definition,
            tool_id,
            metadata,
        )
        if capabilities:
            tools.append(
                SubagentToolSpec(
                    tool_id=tool_id,
                    display_name=metadata.display_name,
                    capabilities=capabilities,
                )
            )

    return SubagentToolProfile(tools=tuple(tools), definition_id=definition.id)


def resolve_subagent_tool_ids(
    definition: SubagentDefinition,
    visible_tool_ids: Iterable[Any] | None = None,
) -> tuple[str, ...]:
    """Return definition-visible tool ids for graph binding code."""

    return resolve_subagent_tool_profile(definition, visible_tool_ids).tool_ids


def is_subagent_tool_allowed(
    definition: SubagentDefinition,
    tool_id: Any,
) -> bool:
    """Return whether a tool id is allowed by the resolved definition profile."""

    normalized = _normalize_tool_id(tool_id)
    if not normalized:
        return False
    return normalized in resolve_subagent_tool_ids(definition)


def subagent_capabilities_from_metadata(
    definition: SubagentDefinition,
    tool_id: Any,
    metadata: EnhancedToolMetadata,
) -> tuple[str, ...]:
    """Return normalized capabilities only when metadata proves profile safety."""

    normalized_tool_id = _normalize_tool_id(tool_id)
    if (
        not normalized_tool_id
        or normalized_tool_id not in definition.tool_ids
        or _is_platform_forbidden_tool_id(normalized_tool_id)
        or metadata.category in _PLATFORM_FORBIDDEN_CATEGORIES
        or get_tool_catalog_role(normalized_tool_id) is not ToolCatalogRole.PENTEST
    ):
        return ()

    capabilities = _normalize_capabilities(
        (capability.name for capability in metadata.capabilities),
        definition=definition,
    )
    if not capabilities:
        return ()
    return capabilities


def _normalize_capabilities(
    capability_names: Iterable[Any],
    *,
    definition: SubagentDefinition,
) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_name in capability_names:
        capability = resolve_definition_capability(definition, raw_name)
        if capability is None or capability in normalized:
            continue
        normalized.append(capability)
    return tuple(normalized)


def _is_platform_forbidden_tool_id(tool_id: str) -> bool:
    return tool_id.startswith(_PLATFORM_FORBIDDEN_TOOL_PREFIXES)


def _normalize_tool_id(tool_id: Any) -> str:
    return str(tool_id or "").strip()


__all__ = [
    "SubagentToolProfile",
    "SubagentToolSpec",
    "is_subagent_tool_allowed",
    "resolve_subagent_tool_ids",
    "resolve_subagent_tool_profile",
    "subagent_capabilities_from_metadata",
]
