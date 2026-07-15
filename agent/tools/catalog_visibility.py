"""Tool-catalog visibility policy for LLM-facing tool exposure.

This module centralizes which tools are visible in tool catalogs used by
planning/selection prompts. Non-visible tools remain implemented and callable
by direct/internal flows, but are not surfaced for LLM self-selection.

Ownership boundary: the tool registry owns implemented tools, this module owns
prompt/catalog visibility, and artifact exposure remains a separate runtime
overlay.
"""

from __future__ import annotations

from typing import Any, Iterable, List

_VISIBLE_TOOL_IDS: frozenset[str] = frozenset(
    {
        "filesystem.append_file",
        "filesystem.copy_path",
        "filesystem.delete_path",
        "filesystem.edit_lines",
        "filesystem.find_paths",
        "filesystem.grep",
        "filesystem.list_dir",
        "filesystem.make_dir",
        "filesystem.move_path",
        "filesystem.read_file",
        "filesystem.read_head",
        "filesystem.read_tail",
        "filesystem.search_text",
        "filesystem.stat_path",
        "filesystem.write_file",
        "exploitation_tools.metasploit.inspect_module",
        "exploitation_tools.metasploit.run_exploit",
        "exploitation_tools.metasploit.search_modules",
        "information_gathering.network_discovery.fping",
        "information_gathering.network_discovery.nmap",
        "information_gathering.web_enumeration.http_download",
        "information_gathering.web_enumeration.http_request",
        "networking_utilities.network",
        "service_access.ftp_download",
        "service_access.ftp_list",
        "service_access.ftp_login",
        "service_access.ssh_login",
        "sniffing_spoofing.network_sniffers.tshark",
        "web_applications.web_crawlers.ffuf",
    }
)


def _normalize_tool_id(tool_id: Any) -> str:
    """Return a stripped string tool id, or an empty string for missing input."""
    return str(tool_id or "").strip()


def is_tool_hidden_from_catalog(tool_id: str) -> bool:
    """Return whether a tool should be excluded from LLM tool catalogs."""
    normalized = _normalize_tool_id(tool_id)
    if not normalized:
        return False
    return normalized not in _VISIBLE_TOOL_IDS


def is_tool_visible_in_catalog(
    tool_id: Any,
    *,
    include_artifact_tools: bool = False,
) -> bool:
    """Return whether a tool should be included in LLM-facing catalogs."""
    _ = include_artifact_tools
    normalized = _normalize_tool_id(tool_id)
    if not normalized:
        return False
    return normalized in _VISIBLE_TOOL_IDS


def filter_visible_tool_ids(
    tool_ids: Iterable[Any],
    *,
    include_artifact_tools: bool = False,
) -> List[str]:
    """Return stable, deduped tool ids that are visible in LLM-facing catalogs."""
    visible_tools: List[str] = []
    for raw_tool_id in tool_ids:
        tool_id = _normalize_tool_id(raw_tool_id)
        if not tool_id or tool_id in visible_tools:
            continue
        if is_tool_visible_in_catalog(
            tool_id,
            include_artifact_tools=include_artifact_tools,
        ):
            visible_tools.append(tool_id)
    return visible_tools


def visible_available_tools() -> List[str]:
    """Return currently registered tools that are visible in LLM-facing catalogs."""
    try:
        from .tool_registry import available_tools

        return filter_visible_tool_ids(available_tools())
    except Exception:
        return []


__all__ = [
    "filter_visible_tool_ids",
    "is_tool_hidden_from_catalog",
    "is_tool_visible_in_catalog",
    "visible_available_tools",
]
