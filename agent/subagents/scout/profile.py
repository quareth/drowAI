"""Least-privilege tool profile for the Scout recon subagent.

This module resolves the migration-free pilot's Scout tool allowlist from the
currently registered, LLM-visible tool catalog while enforcing a small bounded
network-recon ceiling. It does not execute tools, mutate runtime state, or
authorize targets.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from agent.subagents.contracts import ReconCapability
from agent.tools.catalog_policy import get_tool_catalog_role
from agent.tools.catalog_visibility import visible_available_tools
from agent.tools.categories import ToolCategory
from agent.tools.enhanced_metadata import EnhancedToolMetadata, ToolCatalogRole
from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata


SCOUT_RECON_TOOL_ID_CEILING: frozenset[str] = frozenset(
    {
        "information_gathering.network_discovery.fping",
        "information_gathering.network_discovery.nmap",
    }
)
SCOUT_OWNED_CAPABILITIES: frozenset[ReconCapability] = frozenset(
    {"host_discovery", "port_scan", "service_enum"}
)

_SCOUT_ALLOWED_CATEGORIES: frozenset[ToolCategory] = frozenset(
    {ToolCategory.NETWORK_DISCOVERY}
)
_CAPABILITY_ALIASES: dict[str, ReconCapability] = {
    "host_discovery": "host_discovery",
    "port_discovery": "port_scan",
    "port_scan": "port_scan",
    "port_scanning": "port_scan",
    "service_detection": "service_enum",
    "service_discovery": "service_enum",
    "service_enum": "service_enum",
    "service_enumeration": "service_enum",
}
_FORBIDDEN_CATEGORIES: frozenset[ToolCategory] = frozenset(
    {
        ToolCategory.APPLICATION_PROXY,
        ToolCategory.CMS_IDENTIFICATION,
        ToolCategory.DATABASE_ASSESSMENT,
        ToolCategory.EXPLOITATION_TOOLS,
        ToolCategory.FORENSICS,
        ToolCategory.FUZZING,
        ToolCategory.KNOWLEDGE,
        ToolCategory.MAINTAINING_ACCESS,
        ToolCategory.OPENVAS_SCANNING,
        ToolCategory.PASSWORD_ATTACKS,
        ToolCategory.REPORTING_TOOLS,
        ToolCategory.REVERSE_ENGINEERING,
        ToolCategory.SERVICE_ACCESS,
        ToolCategory.SHELL,
        ToolCategory.SNIFFING_SPOOFING,
        ToolCategory.STRESS_TESTING,
        ToolCategory.VOIP_ANALYSIS,
        ToolCategory.WEB_CRAWLING,
        ToolCategory.WEB_ENUMERATION,
        ToolCategory.WEB_FUZZING,
        ToolCategory.WEB_VULNERABILITY_SCANNING,
        ToolCategory.WORKSPACE_FILESYSTEM,
    }
)
_FORBIDDEN_TOOL_PREFIXES: tuple[str, ...] = (
    "artifact.",
    "exploitation_tools.",
    "filesystem.",
    "knowledge.",
    "password_attacks.",
    "reporting_tools.",
    "service_access.",
    "shell.",
)
_FORBIDDEN_CAPABILITY_TOKENS: tuple[str, ...] = (
    "agent",
    "credential",
    "delete",
    "download",
    "exploit",
    "file",
    "login",
    "metasploit",
    "password",
    "report",
    "shell",
    "write",
)


@dataclass(frozen=True, slots=True)
class ScoutToolSpec:
    """A Scout-visible tool with normalized owned recon capabilities."""

    tool_id: str
    display_name: str
    scout_capabilities: tuple[ReconCapability, ...]


@dataclass(frozen=True, slots=True)
class ScoutToolProfile:
    """Resolved Scout profile used by future tool-selection graph nodes."""

    tools: tuple[ScoutToolSpec, ...]

    @property
    def tool_ids(self) -> tuple[str, ...]:
        """Return Scout-visible tool ids in deterministic catalog order."""

        return tuple(tool.tool_id for tool in self.tools)

    def capabilities_for_tool(self, tool_id: str) -> tuple[ReconCapability, ...]:
        """Return normalized owned capabilities for a Scout-visible tool id."""

        normalized = _normalize_tool_id(tool_id)
        for tool in self.tools:
            if tool.tool_id == normalized:
                return tool.scout_capabilities
        return ()


def resolve_scout_tool_profile(
    visible_tool_ids: Iterable[Any] | None = None,
) -> ScoutToolProfile:
    """Resolve the bounded Scout profile from visible registered metadata."""

    candidate_ids = (
        visible_available_tools() if visible_tool_ids is None else visible_tool_ids
    )
    tools: list[ScoutToolSpec] = []
    seen: set[str] = set()
    for raw_tool_id in candidate_ids:
        tool_id = _normalize_tool_id(raw_tool_id)
        if not tool_id or tool_id in seen:
            continue
        seen.add(tool_id)

        metadata = get_enhanced_tool_metadata(tool_id)
        if metadata is None:
            continue

        capabilities = scout_capabilities_from_metadata(tool_id, metadata)
        if capabilities:
            tools.append(
                ScoutToolSpec(
                    tool_id=tool_id,
                    display_name=metadata.display_name,
                    scout_capabilities=capabilities,
                )
            )

    return ScoutToolProfile(tools=tuple(tools))


def resolve_scout_tool_ids(
    visible_tool_ids: Iterable[Any] | None = None,
) -> tuple[str, ...]:
    """Return Scout-visible tool ids for graph binding code."""

    return resolve_scout_tool_profile(visible_tool_ids).tool_ids


def is_scout_tool_allowed(tool_id: Any) -> bool:
    """Return whether a tool id is currently allowed by the Scout profile."""

    normalized = _normalize_tool_id(tool_id)
    if not normalized:
        return False
    return normalized in resolve_scout_tool_ids()


def scout_capabilities_from_metadata(
    tool_id: Any,
    metadata: EnhancedToolMetadata,
) -> tuple[ReconCapability, ...]:
    """Return normalized Scout capabilities only when metadata proves safety."""

    normalized_tool_id = _normalize_tool_id(tool_id)
    if (
        not normalized_tool_id
        or normalized_tool_id not in SCOUT_RECON_TOOL_ID_CEILING
    ):
        return ()
    if _is_forbidden_tool_id(normalized_tool_id):
        return ()
    if metadata.category not in _SCOUT_ALLOWED_CATEGORIES:
        return ()
    if metadata.category in _FORBIDDEN_CATEGORIES:
        return ()
    if get_tool_catalog_role(normalized_tool_id) is not ToolCatalogRole.PENTEST:
        return ()

    capabilities = _normalize_scout_capabilities(
        capability.name for capability in metadata.capabilities
    )
    if not capabilities:
        return ()
    if any(
        _contains_forbidden_capability_token(capability.name)
        for capability in metadata.capabilities
    ):
        return ()
    return capabilities


def _normalize_scout_capabilities(
    capability_names: Iterable[Any],
) -> tuple[ReconCapability, ...]:
    normalized: list[ReconCapability] = []
    for raw_name in capability_names:
        capability = _CAPABILITY_ALIASES.get(_normalize_token(raw_name))
        if capability is None or capability in normalized:
            continue
        if capability not in SCOUT_OWNED_CAPABILITIES:
            continue
        normalized.append(capability)
    return tuple(normalized)


def _is_forbidden_tool_id(tool_id: str) -> bool:
    return tool_id.startswith(_FORBIDDEN_TOOL_PREFIXES)


def _contains_forbidden_capability_token(capability_name: Any) -> bool:
    normalized = _normalize_token(capability_name)
    return any(token in normalized for token in _FORBIDDEN_CAPABILITY_TOKENS)


def _normalize_tool_id(tool_id: Any) -> str:
    return str(tool_id or "").strip()


def _normalize_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


__all__ = [
    "SCOUT_OWNED_CAPABILITIES",
    "SCOUT_RECON_TOOL_ID_CEILING",
    "ScoutToolProfile",
    "ScoutToolSpec",
    "is_scout_tool_allowed",
    "resolve_scout_tool_ids",
    "resolve_scout_tool_profile",
    "scout_capabilities_from_metadata",
]
