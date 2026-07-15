"""Tests for shared LLM-facing tool catalog visibility helpers."""

from __future__ import annotations

from agent.tools import catalog_visibility
from agent.tools.catalog_policy import get_tool_catalog_role, is_user_configurable_tool
from agent.tools.enhanced_metadata import ToolCatalogRole

MVP_VISIBLE_TOOLS = [
    "networking_utilities.network",
    "service_access.ftp_login",
    "service_access.ftp_list",
    "service_access.ftp_download",
    "service_access.ssh_login",
    "information_gathering.network_discovery.nmap",
    "information_gathering.network_discovery.fping",
    "information_gathering.web_enumeration.http_request",
    "information_gathering.web_enumeration.http_download",
    "exploitation_tools.metasploit.search_modules",
    "exploitation_tools.metasploit.inspect_module",
    "exploitation_tools.metasploit.run_exploit",
    "web_applications.web_crawlers.ffuf",
    "sniffing_spoofing.network_sniffers.tshark",
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
]


def test_hidden_and_visible_predicates_match_mvp_allowlist_policy() -> None:
    assert catalog_visibility.is_tool_visible_in_catalog("") is False
    assert catalog_visibility.is_tool_visible_in_catalog(None) is False

    assert catalog_visibility.is_tool_hidden_from_catalog("shell.exec") is True
    assert catalog_visibility.is_tool_visible_in_catalog("shell.exec") is False

    assert catalog_visibility.is_tool_hidden_from_catalog("filesystem.grep") is False
    assert catalog_visibility.is_tool_visible_in_catalog("filesystem.grep") is True

    assert catalog_visibility.is_tool_hidden_from_catalog("filesystem.read_file") is False
    assert catalog_visibility.is_tool_visible_in_catalog("filesystem.read_file") is True

    assert catalog_visibility.is_tool_hidden_from_catalog("run_kali_utility") is True
    assert catalog_visibility.is_tool_visible_in_catalog("run_kali_utility") is False

    for tool_id in (
        "service_access.ftp_login",
        "service_access.ftp_list",
        "service_access.ftp_download",
        "service_access.ssh_login",
    ):
        assert catalog_visibility.is_tool_hidden_from_catalog(tool_id) is False
        assert catalog_visibility.is_tool_visible_in_catalog(tool_id) is True

    assert (
        catalog_visibility.is_tool_hidden_from_catalog(
            "information_gathering.network_discovery.fping"
        )
        is False
    )
    assert (
        catalog_visibility.is_tool_visible_in_catalog(
            "information_gathering.network_discovery.fping"
        )
        is True
    )
    assert (
        catalog_visibility.is_tool_hidden_from_catalog(
            "information_gathering.network_discovery.masscan"
        )
        is True
    )
    assert (
        catalog_visibility.is_tool_visible_in_catalog(
            "information_gathering.network_discovery.masscan"
        )
        is False
    )
    assert (
        catalog_visibility.is_tool_hidden_from_catalog(
            "exploitation_tools.metasploit.run_exploit"
        )
        is False
    )
    assert (
        catalog_visibility.is_tool_visible_in_catalog(
            "exploitation_tools.metasploit.run_exploit"
        )
        is True
    )
    assert (
        catalog_visibility.is_tool_hidden_from_catalog(
            "password_attacks.online_attacks.hydra"
        )
        is True
    )
    assert (
        catalog_visibility.is_tool_visible_in_catalog(
            "password_attacks.online_attacks.hydra"
        )
        is False
    )


def test_filter_visible_tool_ids_is_stable_deduped_and_stripped() -> None:
    result = catalog_visibility.filter_visible_tool_ids(
        [
            " shell.exec ",
            "filesystem.read_file",
            "filesystem.grep",
            "",
            None,
            "service_access.ftp_login",
            "information_gathering.network_discovery.fping",
            "information_gathering.network_discovery.masscan",
            "password_attacks.online_attacks.hydra",
            "filesystem.read_file",
            "vulnerability_analysis.openvas.openvas",
            "information_gathering.network_discovery.nmap",
        ]
    )

    assert result == [
        "filesystem.read_file",
        "filesystem.grep",
        "service_access.ftp_login",
        "information_gathering.network_discovery.fping",
        "information_gathering.network_discovery.nmap",
    ]


def test_visible_available_tools_delegates_to_registry_and_visibility(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tools.tool_registry.available_tools",
        lambda: [
            "shell.exec",
            "filesystem.read_file",
            "service_access.ftp_login",
            "shell.script",
            "filesystem.grep",
            "filesystem.read_file",
        ],
    )

    assert catalog_visibility.visible_available_tools() == [
        "filesystem.read_file",
        "service_access.ftp_login",
        "filesystem.grep",
    ]


def test_visible_available_tools_returns_only_mvp_tools() -> None:
    assert set(catalog_visibility.visible_available_tools()) == set(MVP_VISIBLE_TOOLS)


def test_artifact_tools_stay_hidden_even_when_legacy_overlay_flag_is_set() -> None:
    assert catalog_visibility.is_tool_visible_in_catalog("artifact.search") is False
    assert (
        catalog_visibility.is_tool_visible_in_catalog(
            "artifact.search",
            include_artifact_tools=True,
        )
        is False
    )
    assert catalog_visibility.filter_visible_tool_ids(
        ["artifact.search", "filesystem.read_file"],
        include_artifact_tools=True,
    ) == ["filesystem.read_file"]


def test_http_download_is_visible_utility_not_user_configurable_pentest() -> None:
    tool_id = "information_gathering.web_enumeration.http_download"

    assert catalog_visibility.is_tool_visible_in_catalog(tool_id) is True
    assert get_tool_catalog_role(tool_id) == ToolCatalogRole.UTILITY
    assert is_user_configurable_tool(tool_id) is False


def test_fping_and_metasploit_are_visible_pentest_tools() -> None:
    for tool_id in (
        "information_gathering.network_discovery.fping",
        "exploitation_tools.metasploit.search_modules",
        "exploitation_tools.metasploit.inspect_module",
        "exploitation_tools.metasploit.run_exploit",
    ):
        assert catalog_visibility.is_tool_visible_in_catalog(tool_id) is True
        assert get_tool_catalog_role(tool_id) == ToolCatalogRole.PENTEST
        assert is_user_configurable_tool(tool_id) is True
