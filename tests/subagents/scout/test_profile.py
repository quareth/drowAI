"""Tests for the Scout recon subagent tool profile."""

from __future__ import annotations

from agent.subagents.scout.profile import (
    SCOUT_RECON_TOOL_ID_CEILING,
    is_scout_tool_allowed,
    resolve_scout_tool_profile,
    scout_capabilities_from_metadata,
)
from agent.tools.categories import ToolCategory
from agent.tools.enhanced_metadata import EnhancedToolMetadata, ToolCapability
from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata


def test_scout_profile_resolves_exact_bounded_recon_tools() -> None:
    profile = resolve_scout_tool_profile()

    assert set(profile.tool_ids) == SCOUT_RECON_TOOL_ID_CEILING
    assert profile.capabilities_for_tool(
        "information_gathering.network_discovery.fping"
    ) == ("host_discovery",)
    assert profile.capabilities_for_tool(
        "information_gathering.network_discovery.nmap"
    ) == ("port_scan", "service_enum")


def test_scout_profile_excludes_visible_non_owned_tools() -> None:
    visible_non_scout_tools = [
        "exploitation_tools.metasploit.run_exploit",
        "filesystem.write_file",
        "information_gathering.dns.amass",
        "information_gathering.web_enumeration.http_request",
        "networking_utilities.network",
        "service_access.ssh_login",
        "sniffing_spoofing.network_sniffers.tshark",
        "web_applications.web_crawlers.ffuf",
    ]

    profile = resolve_scout_tool_profile(visible_non_scout_tools)

    assert profile.tool_ids == ()
    for tool_id in visible_non_scout_tools:
        assert not is_scout_tool_allowed(tool_id)


def test_scout_capabilities_require_ceiling_even_for_recon_like_metadata() -> None:
    metadata = EnhancedToolMetadata(
        tool_id="information_gathering.network_discovery.future_scanner",
        display_name="Future Scanner",
        category=ToolCategory.NETWORK_DISCOVERY,
        capabilities=[
            ToolCapability(
                name="host_discovery",
                description="Discovers hosts",
            )
        ],
    )

    assert (
        scout_capabilities_from_metadata(
            "information_gathering.network_discovery.future_scanner",
            metadata,
        )
        == ()
    )


def test_scout_profile_requires_registered_metadata() -> None:
    profile = resolve_scout_tool_profile(
        [
            "information_gathering.network_discovery.fping",
            "information_gathering.network_discovery.not_registered",
        ]
    )

    assert profile.tool_ids == ("information_gathering.network_discovery.fping",)


def test_current_ceiling_tools_have_registered_metadata() -> None:
    for tool_id in SCOUT_RECON_TOOL_ID_CEILING:
        assert get_enhanced_tool_metadata(tool_id) is not None
