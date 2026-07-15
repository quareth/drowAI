"""Tests for visible tool category extraction and filtering."""

from __future__ import annotations

from types import SimpleNamespace

from agent.tools.category_utils import get_tool_categories, get_tools_for_categories

MVP_VISIBLE_CATEGORIES = [
    "exploitation_tools",
    "filesystem",
    "information_gathering",
    "networking_utilities",
    "service_access",
    "sniffing_spoofing",
    "web_applications",
]


def test_get_tool_categories_uses_visible_tools(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tools.tool_registry.available_tools",
        lambda: [
            "shell.exec",
            "filesystem.read_file",
            "information_gathering.network_discovery.nmap",
        ],
    )

    assert get_tool_categories() == ["filesystem", "information_gathering"]


def test_get_tools_for_categories_uses_visible_tools_before_sorting(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tools.tool_registry.available_tools",
        lambda: [
            "shell.exec",
            "filesystem.read_file",
            "filesystem.search_text",
        ],
    )
    monkeypatch.setattr(
        "agent.tools.enhanced_metadata_registry.get_enhanced_tool_metadata",
        lambda _tool_id: SimpleNamespace(execution_priority=5),
    )

    assert get_tools_for_categories(["shell", "filesystem"]) == [
        "filesystem.read_file",
        "filesystem.search_text",
    ]


def test_web_applications_category_exposes_visible_http_web_tools(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tools.tool_registry.available_tools",
        lambda: [
            "information_gathering.network_discovery.nmap",
            "information_gathering.web_enumeration.http_request",
            "information_gathering.web_enumeration.http_download",
            "web_applications.web_crawlers.ffuf",
        ],
    )
    monkeypatch.setattr(
        "agent.tools.enhanced_metadata_registry.get_enhanced_tool_metadata",
        lambda _tool_id: SimpleNamespace(execution_priority=5),
    )

    tools = get_tools_for_categories(["web_applications"])

    assert set(tools) == {
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
        "web_applications.web_crawlers.ffuf",
    }
    assert "information_gathering.network_discovery.nmap" not in tools


def test_service_access_category_uses_visible_tool_namespace() -> None:
    assert set(get_tools_for_categories(["service_access"])) == {
        "service_access.ftp_login",
        "service_access.ftp_list",
        "service_access.ftp_download",
        "service_access.ssh_login",
    }


def test_runtime_tool_categories_reflect_mvp_visible_tool_set() -> None:
    assert get_tool_categories() == MVP_VISIBLE_CATEGORIES
