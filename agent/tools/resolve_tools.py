"""resolve_tools handler built on ContextualToolSelector.

Given a capability string (typically matching ActionType values) and context,
return a small set of relevant tool IDs using the existing selection logic.
"""

from __future__ import annotations

import logging  # noqa: E402
from typing import Any, Dict, List, Union  # noqa: E402

try:
    from agent.models import ActionType
except ImportError:  # pragma: no cover
    from ..models import ActionType  # type: ignore

CapabilityType = None  # type: ignore[assignment, misc]

from .action_mapper import ContextualToolSelector  # noqa: E402
from .tool_registry import available_tools  # noqa: E402
from runtime_shared.shell_capabilities import (  # noqa: E402
    SHELL_ASSESSMENT_TOOL_ID,
    SHELL_EXEC_TOOL_ID,
    SHELL_UTILITY_TOOL_ID,
)

logger = logging.getLogger(__name__)


def _get_capability_type_enum():
    """Return CapabilityType enum with a resilient lazy import."""
    global CapabilityType
    if CapabilityType is not None:
        return CapabilityType
    try:
        from agent.graph.infrastructure.state_models import CapabilityType as CapabilityTypeEnum

        CapabilityType = CapabilityTypeEnum
        return CapabilityTypeEnum
    except Exception as exc:  # pragma: no cover - defensive for partial runtime bootstraps
        logger.warning("[CAPABILITY] Failed to import CapabilityType: %s", exc)
        return None


def _get_tools_for_capability(capability_type: "CapabilityType", context: Dict[str, Any]) -> List[str]:
    """Get tools directly from CapabilityType using tool categories.
    
    Approach:
    - Use CapabilityType.get_tool_categories() to get relevant tool categories
    - Select tools from those categories using ContextualToolSelector
    - No hardcoded CapabilityType → ActionType mapping
    
    Args:
        capability_type: CapabilityType enum value
        context: Context dict for tool selection
    
    Returns:
        List of tool IDs
    """
    capability_enum = _get_capability_type_enum()
    if not capability_enum or not capability_type:
        return []
    
    # Get tool categories for this capability
    tool_categories = capability_type.get_tool_categories()
    
    # If no tool categories (e.g., RESPOND), return empty list
    if not tool_categories:
        logger.debug(
            f"[CAPABILITY] Capability '{capability_type.value}' does not require tools"
        )
        return []
    
    # Use ContextualToolSelector to get tools from these categories
    selector = ContextualToolSelector()
    all_candidates = []
    
    # TEMPORARY BRIDGE: ContextualToolSelector still uses ActionType internally
    # Future: Refactor ContextualToolSelector to work directly with tool categories
    # For now, use minimal ActionType hints to preserve tool selection logic
    category_to_action_hint = {
        "artifact": ActionType.GATHER_INFO,
        "database_assessment": ActionType.ENUMERATE_SERVICES,
        "exploitation_tools": ActionType.TEST_EXPLOIT,
        "filesystem": ActionType.GATHER_INFO,
        "forensics": ActionType.GATHER_INFO,
        "information_gathering": ActionType.SCAN_PORTS,
        "knowledge": ActionType.GATHER_INFO,
        "maintaining_access": ActionType.TEST_EXPLOIT,
        "password_attacks": ActionType.TEST_EXPLOIT,
        "reporting_tools": ActionType.GENERATE_REPORT,
        "reverse_engineering": ActionType.GATHER_INFO,
        "shell": ActionType.GATHER_INFO,
        "sniffing_spoofing": ActionType.GATHER_INFO,
        "stress_testing": ActionType.SCAN_PORTS,
        "system_services": ActionType.ENUMERATE_SERVICES,
        "vulnerability_analysis": ActionType.SCAN_PORTS,
        "web_applications": ActionType.SCAN_WEB,
    }
    
    for category in tool_categories:
        action_hint = category_to_action_hint.get(category, ActionType.GATHER_INFO)
        candidates = selector.select_tools_for_action(action_hint, context)
        all_candidates.extend(candidates)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_tools = []
    for tool_id in all_candidates:
        if tool_id not in seen:
            seen.add(tool_id)
            unique_tools.append(tool_id)
    
    return unique_tools


def resolve_tools_for_capability(
    capability: Union[str, Any],  # CapabilityType enum or string
    context: Dict[str, Any] | None = None,
    config: Any | None = None,
) -> List[str]:
    """Resolve a ranked list of tool IDs for a given capability.

    Approach:
    - Normalize capability string to CapabilityType enum
    - Use CapabilityType.get_tool_categories() to find relevant tool categories
    - Select tools from those categories
    - No hardcoded CapabilityType → ActionType mapping
    
    Args:
        capability: CapabilityType enum or string (will be normalized to CapabilityType)
        context: Optional context dict for tool selection
        config: Optional agent configuration for capability selection
    
    Returns:
        List of tool IDs, empty list if no tools available or capability doesn't require tools
    """
    context = dict(context or {})
    
    max_tools = None
    if config is not None and hasattr(config, "max_tools_per_action"):
        try:
            max_tools = int(getattr(config, "max_tools_per_action"))
        except Exception:
            max_tools = None
    if max_tools is None:
        try:
            max_tools = int(context.get("max_tools_per_action", 3))
        except Exception:
            max_tools = 3
    context.setdefault("max_tools_per_action", max_tools)

    # Normalize capability to CapabilityType
    capability_enum = _get_capability_type_enum()

    if capability_enum and isinstance(capability, capability_enum):
        normalized_cap = capability
    else:
        capability_str = str(capability) if capability else ""
        normalized = capability_str.strip().lower()
        
        # Handle special filesystem/shell capabilities (not in CapabilityType enum)
        filesystem_capabilities = {
            "filesystem",
            "workspace_filesystem",
            "read_file",
            "write_file",
            "append_file",
            "edit_lines",
            "read_head",
            "read_tail",
            "grep",
            "delete_path",
            "make_dir",
            "list_dir",
            "move_path",
            "copy_path",
            "stat_path",
            "find_paths",
            "search_text",
        }
        network_utility_capabilities = {
            "network_utility",
            "ping",
            "dig",
            "dns_lookup",
            "whois",
            "tcp_connect",
            "traceroute",
            "trace_route",
            "interfaces",
            "routes",
        }
        special_capabilities: Dict[str, List[str]] = {
            **{
                capability_name: [SHELL_UTILITY_TOOL_ID]
                for capability_name in filesystem_capabilities
            },
            "shell": [SHELL_UTILITY_TOOL_ID, SHELL_ASSESSMENT_TOOL_ID],
            "shell_exec": [SHELL_UTILITY_TOOL_ID, SHELL_ASSESSMENT_TOOL_ID],
            "shell_script": [SHELL_UTILITY_TOOL_ID],
            "http": [
                "information_gathering.web_enumeration.http_request",
                "information_gathering.web_enumeration.http_download",
            ],
            "http_fetch": ["information_gathering.web_enumeration.http_request"],
            "http_probe": ["information_gathering.web_enumeration.http_request"],
            "http_request": ["information_gathering.web_enumeration.http_request"],
            "http_download": ["information_gathering.web_enumeration.http_download"],
            "curl": [
                "information_gathering.web_enumeration.http_request",
                "information_gathering.web_enumeration.http_download",
            ],
            "download": ["information_gathering.web_enumeration.http_download"],
            "file_download": ["information_gathering.web_enumeration.http_download"],
            "http_debug": [
                "information_gathering.web_enumeration.http_request",
                "information_gathering.web_enumeration.http_download",
            ],
            "http_trace": [
                "information_gathering.web_enumeration.http_request",
                "information_gathering.web_enumeration.http_download",
            ],
            "http_session": [
                "information_gathering.web_enumeration.http_request",
                "information_gathering.web_enumeration.http_download",
            ],
            "http_upload": ["information_gathering.web_enumeration.http_request"],
            "http_tls": [
                "information_gathering.web_enumeration.http_request",
                "information_gathering.web_enumeration.http_download",
            ],
            "http_resilient": [
                "information_gathering.web_enumeration.http_request",
                "information_gathering.web_enumeration.http_download",
            ],
            "http_binary": ["information_gathering.web_enumeration.http_request"],
            "http_protocol": [
                "information_gathering.web_enumeration.http_request",
                "information_gathering.web_enumeration.http_download",
            ],
            "http2": [
                "information_gathering.web_enumeration.http_request",
                "information_gathering.web_enumeration.http_download",
            ],
            "http3": [
                "information_gathering.web_enumeration.http_request",
                "information_gathering.web_enumeration.http_download",
            ],
            **{
                capability_name: [SHELL_UTILITY_TOOL_ID]
                for capability_name in network_utility_capabilities
            },
        }
        
        if normalized in special_capabilities:
            existing = set(available_tools())
            return [tid for tid in special_capabilities[normalized] if tid in existing]
        
        # Normalize to CapabilityType enum
        if not capability_enum:
            logger.warning("[CAPABILITY] CapabilityType not available, cannot resolve tools")
            return []
        
        try:
            normalized_cap = capability_enum.from_intent(capability_str)
            logger.debug(
                f"[CAPABILITY] Resolving tools for capability '{normalized_cap.value}'"
            )
        except Exception as exc:
            logger.warning(
                f"[CAPABILITY] Failed to normalize capability '{capability_str}': {exc}"
            )
            return []
    
    # Get tools using capability-based selection
    candidates = _get_tools_for_capability(normalized_cap, context)
    candidates = _map_hidden_utility_candidates(candidates)
    
    # Apply max_tools limit
    return candidates[: max(1, max_tools)]


def _map_hidden_utility_candidates(candidates: List[str]) -> List[str]:
    """Map hidden general-purpose candidates onto model-facing shell aliases."""

    mapped: List[str] = []
    for raw_tool_id in candidates:
        tool_id = str(raw_tool_id or "").strip()
        if not tool_id:
            continue
        if tool_id == SHELL_EXEC_TOOL_ID:
            replacements = [SHELL_UTILITY_TOOL_ID, SHELL_ASSESSMENT_TOOL_ID]
        elif tool_id.startswith("filesystem.") or tool_id in {
            "networking_utilities.network",
            "shell.script",
        }:
            replacements = [SHELL_UTILITY_TOOL_ID]
        else:
            replacements = [tool_id]
        for replacement in replacements:
            if replacement not in mapped:
                mapped.append(replacement)
    return mapped
