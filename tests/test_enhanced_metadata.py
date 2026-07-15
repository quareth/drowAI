"""Tests for enhanced tool metadata model validation."""

import os
import sys

import pytest

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.tools.categories import PentestPhase, ToolCategory
from agent.tools.enhanced_metadata import (
    EnhancedToolMetadata,
    ToolCapability,
    ToolCatalogRole,
)
from pydantic import ValidationError


def test_enhanced_metadata_schema() -> None:
    metadata = EnhancedToolMetadata(
        tool_id="information_gathering.network_discovery.nmap",
        display_name="Nmap",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[PentestPhase.RECONNAISSANCE],
        capabilities=[
            ToolCapability(
                name="port_scan",
                description="Discover open ports",
                output_indicators=["open"],
            )
        ],
        required_services=[],
        target_protocols=["tcp", "udp"],
        execution_priority=9,
        parallel_compatible=True,
        stealth_level=3,
    )
    assert metadata.tool_id == "information_gathering.network_discovery.nmap"
    assert metadata.catalog_role == ToolCatalogRole.PENTEST
    assert metadata.execution_priority in range(1, 11)
    assert metadata.stealth_level in range(1, 6)


def test_enhanced_metadata_catalog_role_values() -> None:
    """Catalog role accepts the supported role values."""
    utility = EnhancedToolMetadata(
        tool_id="filesystem.read_file",
        display_name="Read File",
        category=ToolCategory.WORKSPACE_FILESYSTEM,
        catalog_role=ToolCatalogRole.UTILITY,
    )
    system = EnhancedToolMetadata(
        tool_id="artifact.read",
        display_name="Artifact Read",
        category=ToolCategory.KNOWLEDGE,
        catalog_role=ToolCatalogRole.SYSTEM,
    )

    assert utility.catalog_role == ToolCatalogRole.UTILITY
    assert system.catalog_role == ToolCatalogRole.SYSTEM


def test_enhanced_metadata_validation() -> None:
    with pytest.raises(ValidationError):
        EnhancedToolMetadata(
            tool_id="information_gathering.network_discovery.nmap",
            display_name="Nmap",
            category=ToolCategory.NETWORK_DISCOVERY,
            execution_priority=11,
        )


def test_enhanced_metadata_rejects_invalid_catalog_role() -> None:
    with pytest.raises(ValidationError):
        EnhancedToolMetadata(
            tool_id="example.tool",
            display_name="Example",
            category=ToolCategory.NETWORK_DISCOVERY,
            catalog_role="operator",
        )
