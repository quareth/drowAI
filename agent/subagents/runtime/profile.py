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
from agent.tools.catalog_visibility import (
    is_tool_visible_in_catalog,
    visible_available_tools,
)
from agent.tools.enhanced_metadata import EnhancedToolMetadata, ToolCatalogRole
from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata
from agent.tools.universal_agent_tools import UNIVERSAL_AGENT_TOOL_IDS


_UNIVERSAL_AGENT_TOOL_ID_SET: frozenset[str] = frozenset(UNIVERSAL_AGENT_TOOL_IDS)


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

    return effective_subagent_tool_profile(
        definition,
        _resolve_profile_specific_tool_profile(definition, visible_tool_ids),
    )


def _resolve_profile_specific_tool_profile(
    definition: SubagentDefinition,
    visible_tool_ids: Iterable[Any] | None = None,
) -> SubagentToolProfile:
    """Resolve only definition-owned mission tools before universal composition."""

    candidate_tool_ids = _normalize_tool_ids(
        visible_available_tools() if visible_tool_ids is None else visible_tool_ids
    )
    tools: list[SubagentToolSpec] = []
    added_tool_ids: set[str] = set()

    for tool_id in candidate_tool_ids:
        if tool_id in _UNIVERSAL_AGENT_TOOL_ID_SET:
            continue

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
            added_tool_ids.add(tool_id)

    return SubagentToolProfile(tools=tuple(tools), definition_id=definition.id)


def effective_subagent_tool_profile(
    definition: SubagentDefinition,
    profile: SubagentToolProfile | Any | None = None,
    visible_tool_ids: Iterable[Any] | None = None,
) -> SubagentToolProfile:
    """Return a profile-specific tool set union the central universal tools."""

    if profile is None:
        profile = _resolve_profile_specific_tool_profile(definition, visible_tool_ids)

    tools: list[SubagentToolSpec] = []
    added_tool_ids: set[str] = set()
    for raw_spec in getattr(profile, "tools", ()):
        tool_id = _normalize_tool_id(getattr(raw_spec, "tool_id", ""))
        display_name = str(getattr(raw_spec, "display_name", "") or "").strip()
        if not tool_id or not display_name or tool_id in added_tool_ids:
            continue
        tools.append(
            SubagentToolSpec(
                tool_id=tool_id,
                display_name=display_name,
                capabilities=tuple(getattr(raw_spec, "capabilities", ()) or ()),
            )
        )
        added_tool_ids.add(tool_id)

    for tool_id in UNIVERSAL_AGENT_TOOL_IDS:
        if tool_id in added_tool_ids:
            continue
        metadata = _registered_visible_universal_tool_metadata(tool_id)
        if metadata is None:
            continue
        tools.append(
            SubagentToolSpec(
                tool_id=tool_id,
                display_name=metadata.display_name,
                capabilities=(),
            )
        )
        added_tool_ids.add(tool_id)

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


def _registered_visible_universal_tool_metadata(
    tool_id: str,
) -> EnhancedToolMetadata | None:
    if not is_tool_visible_in_catalog(tool_id):
        return None
    try:
        from agent.tools.tool_registry import tool_exists

        if not tool_exists(tool_id):
            return None
    except Exception:
        return None
    return get_enhanced_tool_metadata(tool_id)


def _normalize_tool_ids(tool_ids: Iterable[Any]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_tool_id in tool_ids:
        tool_id = _normalize_tool_id(raw_tool_id)
        if not tool_id or tool_id in normalized:
            continue
        normalized.append(tool_id)
    return tuple(normalized)


def _normalize_tool_id(tool_id: Any) -> str:
    return str(tool_id or "").strip()


__all__ = [
    "SubagentToolProfile",
    "SubagentToolSpec",
    "effective_subagent_tool_profile",
    "is_subagent_tool_allowed",
    "resolve_subagent_tool_ids",
    "resolve_subagent_tool_profile",
    "subagent_capabilities_from_metadata",
]
