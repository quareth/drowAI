"""Tests for tool catalog role policy classification."""

from agent.tools import available_tools
from agent.tools.catalog_policy import (
    get_tool_catalog_role,
    is_user_configurable_tool,
    resolve_tool_catalog_role,
)
from agent.tools.enhanced_metadata import ToolCatalogRole


def test_every_available_tool_resolves_to_catalog_role() -> None:
    """Every executable tool should have a role for future UI/API policy."""

    tool_ids = available_tools()

    assert tool_ids
    for tool_id in tool_ids:
        resolution = resolve_tool_catalog_role(tool_id)
        assert resolution.catalog_role in set(ToolCatalogRole)
        assert resolution.role_source in {"metadata", "fallback"}


def test_known_utility_tools_are_not_user_configurable() -> None:
    utility_tool_ids = [
        "filesystem.read_file",
        "filesystem.write_file",
        "networking_utilities.network",
        "reporting_tools.report_generation.serpico",
        "service_access.ftp_login",
        "service_access.ftp_list",
        "service_access.ftp_download",
        "service_access.ssh_login",
        "shell.exec",
    ]

    for tool_id in utility_tool_ids:
        assert get_tool_catalog_role(tool_id) == ToolCatalogRole.UTILITY
        assert not is_user_configurable_tool(tool_id)


def test_known_system_tools_are_not_user_configurable() -> None:
    system_tool_ids = [
        "artifact.read",
        "artifact.search",
        "knowledge.cve_lookup",
    ]

    for tool_id in system_tool_ids:
        assert get_tool_catalog_role(tool_id) == ToolCatalogRole.SYSTEM
        assert not is_user_configurable_tool(tool_id)


def test_known_pentest_tools_are_user_configurable() -> None:
    pentest_tool_ids = [
        "information_gathering.network_discovery.nmap",
        "password_attacks.online_attacks.hydra",
        "sniffing_spoofing.network_sniffers.tshark",
        "web_applications.web_crawlers.ffuf",
    ]

    for tool_id in pentest_tool_ids:
        assert get_tool_catalog_role(tool_id) == ToolCatalogRole.PENTEST
        assert is_user_configurable_tool(tool_id)
