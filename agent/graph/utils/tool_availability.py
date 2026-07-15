"""Tool availability checking and graceful degradation utilities.

This module provides functions to check tool availability for capabilities,
implement fallback logic, and handle graceful degradation when tools are missing.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from backend.services.metrics.utils import safe_inc

try:
    from agent.graph.infrastructure.state_models import CapabilityType
except ImportError:  # pragma: no cover
    CapabilityType = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

# Cache for availability checks (capability -> bool)
_availability_cache: Dict[str, bool] = {}


def are_tools_available(capability: str | CapabilityType) -> bool:
    """Check if any tools support the given capability.
    
    Queries the tool registry to see if any tools are available for the capability.
    Results are cached per capability to avoid repeated queries.
    
    Args:
        capability: CapabilityType enum or string capability name
    
    Returns:
        True if tools are available, False otherwise
    """
    # Normalize capability to string
    if CapabilityType and isinstance(capability, CapabilityType):
        capability_str = capability.value
        capability_enum = capability
    else:
        capability_str = str(capability) if capability else ""
        # Try to normalize to CapabilityType
        if CapabilityType:
            try:
                capability_enum = CapabilityType.from_intent(capability_str)
                capability_str = capability_enum.value
            except Exception:
                capability_enum = None
        else:
            capability_enum = None
    
    # Check cache first
    if capability_str in _availability_cache:
        return _availability_cache[capability_str]
    
    # Query tool registry
    try:
        from agent.tools.resolve_tools import resolve_tools_for_capability
        
        tools = resolve_tools_for_capability(capability_str, context=None, config=None)
        available = len(tools) > 0
        
        # Cache result
        _availability_cache[capability_str] = available
        
        if available:
            logger.debug(
                f"[AVAILABILITY] Tools available for capability '{capability_str}': {len(tools)} tools"
            )
        else:
            logger.info(
                f"[AVAILABILITY] No tools available for capability '{capability_str}'"
            )
            safe_inc("capability_tool_gap")
        
        return available
        
    except Exception as exc:
        logger.warning(
            f"[AVAILABILITY] Failed to check tool availability for '{capability_str}': {exc}"
        )
        # On error, assume unavailable (safer default)
        _availability_cache[capability_str] = False
        return False


def get_fallback_capability(
    capability: str | CapabilityType,
) -> Optional[CapabilityType]:
    """Get fallback capability when primary unavailable.
    
    Defines fallback paths for capabilities when the primary capability
    has no available tools. Fallbacks provide alternative approaches
    that may still satisfy the user's intent.
    
    Args:
        capability: Primary capability (CapabilityType enum or string)
    
    Returns:
        Fallback CapabilityType enum value, or None if no fallback exists
    """
    if not CapabilityType:
        return None
    
    # Normalize to CapabilityType enum
    if isinstance(capability, CapabilityType):
        capability_enum = capability
    else:
        try:
            capability_enum = CapabilityType.from_intent(str(capability))
        except Exception:
            return None
    
    # Define fallback matrix
    fallback_map = {
        CapabilityType.VULN_SCAN: CapabilityType.SERVICE_ENUM,
        CapabilityType.VULN_EXPLOIT: CapabilityType.VULN_SCAN,
        CapabilityType.SERVICE_ENUM: CapabilityType.PORT_SCAN,
        CapabilityType.PORT_SCAN: CapabilityType.HOST_DISCOVERY,
        # HOST_DISCOVERY, REPORT, RESPOND have no fallbacks
    }
    
    fallback = fallback_map.get(capability_enum)
    
    if fallback:
        logger.debug(
            f"[FALLBACK] Fallback for '{capability_enum.value}': {fallback.value}"
        )
    
    return fallback


def get_available_tools_for_capability(
    capability: str | CapabilityType,
    context: Optional[Dict] = None,
    config: Optional[Any] = None,
) -> List[str]:
    """Get list of available tool IDs for a capability.
    
    Args:
        capability: CapabilityType enum or string capability name
        context: Optional context dict for tool selection
        config: Optional config object
    
    Returns:
        List of tool IDs, empty list if no tools available
    """
    # Normalize capability to string
    if CapabilityType and isinstance(capability, CapabilityType):
        capability_str = capability.value
    else:
        capability_str = str(capability) if capability else ""
        if CapabilityType:
            try:
                normalized = CapabilityType.from_intent(capability_str)
                capability_str = normalized.value
            except Exception:
                pass
    
    try:
        from agent.tools.resolve_tools import resolve_tools_for_capability
        
        tools = resolve_tools_for_capability(capability_str, context=context, config=config)
        return tools
        
    except Exception as exc:
        logger.warning(
            f"[AVAILABILITY] Failed to resolve tools for '{capability_str}': {exc}"
        )
        return []


def clear_availability_cache() -> None:
    """Clear the availability cache.
    
    Useful for testing or when tool registry changes.
    """
    global _availability_cache
    _availability_cache.clear()
    logger.debug("[AVAILABILITY] Cleared availability cache")


__all__ = [
    "are_tools_available",
    "get_fallback_capability",
    "get_available_tools_for_capability",
    "clear_availability_cache",
]

