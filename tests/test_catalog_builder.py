"""Tests for shared tool catalog builder extraction."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from agent.tools.catalog_builder import build_full_tool_catalog


def test_build_full_tool_catalog_filters_and_limits(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tools.tool_registry.available_tools",
        lambda: [
            "shell.exec",
            "filesystem.read_file",
            "filesystem.grep",
            "web_applications.web_crawlers.ffuf",
        ],
    )
    config = SimpleNamespace(max_tools_exposed=2)

    result = build_full_tool_catalog(config, logger=logging.getLogger(__name__))

    assert result == ["filesystem.read_file", "filesystem.grep"]


def test_build_full_tool_catalog_no_valid_ids_falls_back(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tools.tool_registry.available_tools",
        lambda: ["metadata", "capabilities", "registry"],
    )
    config = SimpleNamespace(max_tools_exposed=10)

    result = build_full_tool_catalog(config, logger=logging.getLogger(__name__))

    assert result == []


def test_build_full_tool_catalog_includes_visible_service_access(monkeypatch) -> None:
    monkeypatch.setattr(
        "agent.tools.tool_registry.available_tools",
        lambda: [
            "shell.exec",
            "filesystem.read_file",
            "service_access.ftp_login",
            "shell.script",
        ],
    )
    config = SimpleNamespace(max_tools_exposed=10)

    result = build_full_tool_catalog(config, logger=logging.getLogger(__name__))

    assert result == ["filesystem.read_file", "service_access.ftp_login"]
