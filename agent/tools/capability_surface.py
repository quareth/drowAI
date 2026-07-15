"""Build compact prompt-facing agent capability summaries from visible tools.

This module converts a caller-provided visible tool list into broad capability
families. The caller's list is the production authority; the global registry is
used only when no list is provided. Hidden catalog tools are excluded before any
family is advertised to the agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set

from core.tool_capability_taxonomy import (
    CAPABILITY_FAMILY_DESCRIPTIONS,
    ordered_capability_families,
)

from .catalog_visibility import filter_visible_tool_ids, visible_available_tools


_TOOL_ID_TO_FAMILIES: Mapping[str, Sequence[str]] = {
    "information_gathering.network_discovery.nmap": (
        "network_discovery",
        "service_enumeration",
    ),
    "information_gathering.web_enumeration.http_download": (
        "http_web_testing",
        "workspace_file_access",
    ),
    "information_gathering.web_enumeration.http_request": (
        "http_web_testing",
        "service_enumeration",
    ),
    "networking_utilities.network": (
        "network_discovery",
        "dns_reconnaissance",
    ),
    "password_attacks.online_attacks.hydra": ("credential_attack",),
    "service_access.ftp_download": (
        "service_enumeration",
        "workspace_file_access",
    ),
    "service_access.ftp_list": ("service_enumeration",),
    "service_access.ftp_login": ("service_enumeration",),
    "service_access.ssh_login": ("service_enumeration",),
    "sniffing_spoofing.network_sniffers.tshark": (
        "service_enumeration",
        "network_discovery",
    ),
    "web_applications.web_crawlers.ffuf": ("http_web_testing",),
    "exploitation_tools.metasploit.run_exploit": ("exploitation_framework",),
}

_TOP_LEVEL_TO_FAMILIES: Mapping[str, Sequence[str]] = {
    "artifact": ("workspace_file_access",),
    "database_assessment": ("service_enumeration",),
    "exploitation_tools": ("exploitation_framework",),
    "filesystem": ("workspace_file_access",),
    "information_gathering": ("network_discovery", "service_enumeration"),
    "knowledge": ("knowledge_lookup",),
    "password_attacks": ("credential_attack",),
    "reporting_tools": ("reporting",),
    "service_access": ("service_enumeration",),
    "shell": ("shell_execution",),
    "system_services": ("service_enumeration",),
    "vulnerability_analysis": ("service_enumeration",),
    "web_applications": ("http_web_testing",),
}

_PATH_SEGMENT_TO_FAMILIES: Mapping[str, Sequence[str]] = {
    "cms_identification": ("http_web_testing", "service_enumeration"),
    "database_assessment": ("service_enumeration",),
    "dns": ("dns_reconnaissance",),
    "dns_enumeration": ("dns_reconnaissance",),
    "http": ("http_web_testing",),
    "metasploit": ("exploitation_framework",),
    "network_discovery": ("network_discovery",),
    "osint": ("knowledge_lookup",),
    "report_generation": ("reporting",),
    "smtp_analysis": ("service_enumeration",),
    "web_crawlers": ("http_web_testing",),
    "web_enumeration": ("http_web_testing", "service_enumeration"),
    "web_vulnerability_scanners": ("http_web_testing"),
    "workspace_filesystem": ("workspace_file_access",),
}

_CATEGORY_VALUE_TO_FAMILIES: Mapping[str, Sequence[str]] = {
    "network_discovery": ("network_discovery",),
    "dns_enumeration": ("dns_reconnaissance",),
    "web_enumeration": ("http_web_testing", "service_enumeration"),
    "web_crawling": ("http_web_testing",),
    "web_vulnerability_scanning": ("http_web_testing",),
    "web_fuzzing": ("http_web_testing",),
    "application_proxy": ("http_web_testing",),
    "cms_identification": ("http_web_testing", "service_enumeration"),
    "database_assessment": ("service_enumeration",),
    "exploitation_tools": ("exploitation_framework",),
    "password_attacks": ("credential_attack",),
    "service_access": ("service_enumeration",),
    "system_services": ("service_enumeration",),
    "reporting_tools": ("reporting",),
    "knowledge": ("knowledge_lookup",),
    "workspace_filesystem": ("workspace_file_access",),
    "shell": ("shell_execution",),
}

@dataclass(frozen=True)
class CapabilitySurface:
    """Rendered agent capability families and source tools."""

    families: Dict[str, List[str]] = field(default_factory=dict)

    def render(self, *, max_tools_per_family: int = 4) -> str:
        """Return compact prompt text for the agent's advertised capabilities."""
        if not self.families:
            return ""

        lines: List[str] = [
            (
                "These are the broad operational capabilities currently "
                "advertised to this agent, derived from the visible tool set "
                "for this run. Use them as advisory boundaries for recovery "
                "and next-step reasoning; exact tool choice remains owned by "
                "the tool selector/builder."
            )
        ]
        for family in ordered_capability_families(list(self.families)):
            description = CAPABILITY_FAMILY_DESCRIPTIONS.get(family, "")
            tools = self.families.get(family, [])
            shown_tools = tools[: max(1, max_tools_per_family)]
            suffix = ""
            hidden_count = len(tools) - len(shown_tools)
            if hidden_count > 0:
                suffix = f" (+{hidden_count} more)"
            tool_text = ", ".join(shown_tools) + suffix
            lines.append(f"- {family}: {description} Visible tools: {tool_text}")
        return "\n".join(lines)


def _default_visible_tools() -> List[str]:
    """Return visible tools from the registry for fallback/default callers."""
    return visible_available_tools()


def _default_available_tools() -> List[str]:
    """Return all registry tools for include-hidden compatibility."""
    try:
        from .tool_registry import available_tools

        return _dedupe_tool_ids(available_tools())
    except Exception:
        return []


def _dedupe_tool_ids(tool_ids: Iterable[Any]) -> List[str]:
    """Return stable, stripped tool ids."""
    result: List[str] = []
    for raw_tool in tool_ids:
        tool_id = str(raw_tool or "").strip()
        if tool_id and tool_id not in result:
            result.append(tool_id)
    return result


def _metadata_families(tool_id: str) -> Set[str]:
    """Infer capability families from explicit metadata categories only."""
    families: Set[str] = set()
    try:
        from .enhanced_metadata_registry import get_enhanced_tool_metadata

        metadata = get_enhanced_tool_metadata(tool_id)
    except Exception:
        metadata = None

    if metadata is None:
        return families

    category = getattr(metadata, "category", None)
    category_value = getattr(category, "value", None)
    if isinstance(category_value, str):
        families.update(_CATEGORY_VALUE_TO_FAMILIES.get(category_value, ()))

    return families


def _fallback_families(tool_id: str) -> Set[str]:
    """Infer capability families from stable tool-id path structure."""
    families: Set[str] = set(_TOOL_ID_TO_FAMILIES.get(tool_id, ()))
    parts = [part for part in tool_id.split(".") if part]
    if parts:
        families.update(_TOP_LEVEL_TO_FAMILIES.get(parts[0], ()))
    for part in parts:
        families.update(_PATH_SEGMENT_TO_FAMILIES.get(part, ()))
    return families


def build_capability_surface(
    tool_ids: Iterable[Any] | None = None,
    *,
    include_hidden: bool = False,
) -> CapabilitySurface:
    """Build the agent capability surface from caller-visible tools."""
    if tool_ids is None:
        source_tools = _default_available_tools() if include_hidden else _default_visible_tools()
    else:
        source_tools = _dedupe_tool_ids(tool_ids)
    visible_tools = (
        _dedupe_tool_ids(source_tools)
        if include_hidden
        else filter_visible_tool_ids(source_tools)
    )

    family_map: Dict[str, List[str]] = {}
    for tool_id in visible_tools:
        families = (
            set(_TOOL_ID_TO_FAMILIES.get(tool_id, ()))
            or _metadata_families(tool_id)
            or _fallback_families(tool_id)
        )
        for family in families:
            if family not in CAPABILITY_FAMILY_DESCRIPTIONS:
                continue
            family_map.setdefault(family, [])
            if tool_id not in family_map[family]:
                family_map[family].append(tool_id)

    return CapabilitySurface(families=family_map)


def render_capability_surface(
    tool_ids: Iterable[Any] | None = None,
    *,
    include_hidden: bool = False,
) -> str:
    """Render advertised agent capability families for prompt injection."""
    return build_capability_surface(
        tool_ids,
        include_hidden=include_hidden,
    ).render()


__all__ = [
    "CapabilitySurface",
    "build_capability_surface",
    "render_capability_surface",
]
