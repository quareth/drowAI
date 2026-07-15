"""Tool integration subpackage.

This package exposes the public tool framework APIs eagerly while loading
individual concrete tool implementations lazily on attribute access. That keeps
simple imports such as ``agent.tools.parameter_validation`` from importing the
entire tool catalog and pulling in unrelated runtime dependencies.
"""

from importlib import import_module

from .base_tool import BaseTool
from .schemas import BaseToolArgs, ToolResult
from .exceptions import ToolValidationError
from .utils import validate_and_execute_tool
from .tool_registry import (
    register_tool,
    tool_exists,
    get_tool,
    run_tool_by_name,
    available_tools,
    get_tool_metadata,
)
from .enhanced_metadata_registry import (
    get_all_enhanced_metadata,
    get_enhanced_tool_metadata,
    register_enhanced_tool_metadata,
    ToolCatalogRole,
)
from .catalog_policy import (
    get_tool_catalog_role,
    is_user_configurable_tool,
    resolve_tool_catalog_role,
)
# Import utility metadata to register filesystem/shell tools
from . import utility_metadata  # noqa: F401
from .action_mapper import ContextualToolSelector
from .service_matcher import ServiceAwareSelector, ServiceInfo, ServiceInventory
from .parameter_generator import ContextualParameterGenerator
from .compatibility import ToolCompatibilityAnalyzer, CompatibilityLevel
_LAZY_EXPORTS = {
    "NmapTool": ("agent.tools.information_gathering.network_discovery.nmap", "NmapTool"),
    "NmapArgs": ("agent.tools.information_gathering.network_discovery.nmap", "NmapArgs"),
    "parse_nmap_xml": ("agent.tools.information_gathering.network_discovery.nmap", "parse_nmap_xml"),
    "FpingTool": ("agent.tools.information_gathering.network_discovery.fping", "FpingTool"),
    "FpingArgs": ("agent.tools.information_gathering.network_discovery.fping", "FpingArgs"),
    "GobusterTool": ("agent.tools.web_applications.web_crawlers.gobuster", "GobusterTool"),
    "GobusterArgs": ("agent.tools.web_applications.web_crawlers.gobuster", "GobusterArgs"),
    "DirbTool": ("agent.tools.web_applications.web_crawlers.dirb", "DirbTool"),
    "DirbArgs": ("agent.tools.web_applications.web_crawlers.dirb", "DirbArgs"),
    "DNSReconTool": ("agent.tools.information_gathering.dns.dnsrecon", "DNSReconTool"),
    "DNSReconArgs": ("agent.tools.information_gathering.dns.dnsrecon", "DNSReconArgs"),
    "DNSEnumTool": ("agent.tools.information_gathering.dns.dnsenum", "DNSEnumTool"),
    "DNSEnumArgs": ("agent.tools.information_gathering.dns.dnsenum", "DNSEnumArgs"),
}


def __getattr__(name: str):
    """Load concrete tool exports lazily when callers request them."""

    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute_name = target
    module = import_module(module_name)
    value = getattr(module, attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Expose lazy exports during interactive inspection."""

    return sorted(set(globals()) | set(_LAZY_EXPORTS))


__all__ = [
    "BaseTool",
    "BaseToolArgs",
    "ToolResult",
    "ToolValidationError",
    "validate_and_execute_tool",
    "register_tool",
    "tool_exists",
    "get_tool",
    "run_tool_by_name",
    "available_tools",
    "get_tool_metadata",
    "register_enhanced_tool_metadata",
    "get_enhanced_tool_metadata",
    "get_all_enhanced_metadata",
    "ToolCatalogRole",
    "get_tool_catalog_role",
    "is_user_configurable_tool",
    "resolve_tool_catalog_role",
    "ContextualToolSelector",
    "ServiceAwareSelector",
    "ServiceInfo",
    "ServiceInventory",
    "ContextualParameterGenerator",
    "ToolCompatibilityAnalyzer",
    "CompatibilityLevel",
    "NmapTool",
    "NmapArgs",
    "parse_nmap_xml",
    "FpingTool",
    "FpingArgs",
    "GobusterTool",
    "GobusterArgs",
    "DirbTool",
    "DirbArgs",
    "DNSReconTool",
    "DNSReconArgs",
    "DNSEnumTool",
    "DNSEnumArgs",
]
