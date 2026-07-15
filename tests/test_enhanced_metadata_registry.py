"""Tests for enhanced tool metadata registration and lookup helpers."""

import os
import sys

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.tools.categories import PentestPhase, ToolCategory
from agent.tools.enhanced_metadata import (
    EnhancedToolMetadata,
    ToolCapability,
    ToolCatalogRole,
)
from agent.tools.enhanced_metadata_registry import (
    get_all_enhanced_metadata,
    get_enhanced_tool_metadata,
    register_enhanced_tool_metadata,
)


def test_registry_prepopulated() -> None:
    """Ensure core tools are registered in the metadata registry."""
    nmap = get_enhanced_tool_metadata("information_gathering.network_discovery.nmap")
    assert nmap is not None
    assert nmap.display_name == "Nmap"

    gobuster = get_enhanced_tool_metadata("web_applications.web_crawlers.gobuster")
    assert gobuster is not None
    assert gobuster.category == ToolCategory.WEB_CRAWLING

    dnsrecon = get_enhanced_tool_metadata("information_gathering.dns.dnsrecon")
    assert dnsrecon is not None
    assert PentestPhase.RECONNAISSANCE in dnsrecon.applicable_phases


def test_register_enhanced_tool_metadata() -> None:
    """Metadata registration should expose entries via lookup helpers."""
    metadata = EnhancedToolMetadata(
        tool_id="utilities.networking.netcat",
        display_name="Netcat",
        category=ToolCategory.NETWORKING_UTILITIES,
        applicable_phases=[PentestPhase.EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="port_forwarding",
                description="Forward ports between hosts",
            )
        ],
        required_services=[],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=True,
        stealth_level=1,
    )

    register_enhanced_tool_metadata(metadata)
    retrieved = get_enhanced_tool_metadata("utilities.networking.netcat")
    assert retrieved is not None
    assert retrieved.display_name == "Netcat"
    assert "utilities.networking.netcat" in get_all_enhanced_metadata()


def test_service_access_metadata_matches_catalog_policy() -> None:
    """service_access metadata should describe bounded single-service actions."""
    metadata = get_enhanced_tool_metadata("service_access.ftp_login")

    assert metadata is not None
    assert metadata.display_name == "FTP Login Proof"
    assert metadata.catalog_role == ToolCatalogRole.UTILITY
    assert metadata.supported_transports == ["file-comm", "pty"]
    assert metadata.category == ToolCategory.SERVICE_ACCESS

    descriptions = " ".join(
        capability.description for capability in metadata.capabilities
    ).lower()
    assert "authenticate to one ftp service" in descriptions
    assert "no brute force" in descriptions
    assert "password123" not in descriptions
