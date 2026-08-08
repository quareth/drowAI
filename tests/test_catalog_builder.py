"""Tests for shared tool catalog builder extraction."""

from __future__ import annotations

import logging
from agent.tools.catalog_builder import build_full_tool_catalog


def test_build_full_tool_catalog_filters_without_limiting_visible_tools(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tools.tool_registry.available_tools",
        lambda: [
            "shell.utility",
            "shell.assessment",
            "shell.write_stdin",
            "information_gathering.network_discovery.nmap",
            "filesystem.read_file",
            "filesystem.grep",
        ],
    )

    result = build_full_tool_catalog(logger=logging.getLogger(__name__))

    assert result == [
        "shell.utility",
        "shell.assessment",
        "shell.write_stdin",
        "information_gathering.network_discovery.nmap",
    ]


def test_default_catalog_preserves_all_visible_tools(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tools.tool_registry.available_tools",
        lambda: [
            "filesystem.read_file",
            "filesystem.grep",
            "filesystem.search_text",
            "shell.script",
            "web_applications.web_crawlers.ffuf",
            "shell.utility",
            "shell.assessment",
            "shell.write_stdin",
            "information_gathering.network_discovery.nmap",
        ],
    )

    result = build_full_tool_catalog(logger=logging.getLogger(__name__))

    assert result == [
        "web_applications.web_crawlers.ffuf",
        "shell.utility",
        "shell.assessment",
        "shell.write_stdin",
        "information_gathering.network_discovery.nmap",
    ]
    assert "shell.script" not in result


def test_build_full_tool_catalog_no_valid_ids_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tools.tool_registry.available_tools",
        lambda: ["metadata", "capabilities", "registry"],
    )
    result = build_full_tool_catalog(logger=logging.getLogger(__name__))

    assert result == []


def test_build_full_tool_catalog_includes_visible_service_access(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tools.tool_registry.available_tools",
        lambda: [
            "shell.utility",
            "shell.assessment",
            "shell.write_stdin",
            "filesystem.read_file",
            "service_access.ftp_login",
            "shell.script",
        ],
    )
    result = build_full_tool_catalog(logger=logging.getLogger(__name__))

    assert result == [
        "shell.utility",
        "shell.assessment",
        "shell.write_stdin",
        "service_access.ftp_login",
    ]
