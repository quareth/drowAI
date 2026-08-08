"""Regression tests for planner-service tool catalog visibility filtering."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import agent.graph.builders  # noqa: F401
from agent.graph.subgraphs.tool_execution_runtime import planner_service


def test_full_planner_catalog_uses_mvp_allowlist(monkeypatch) -> None:
    """Only MVP-visible tools are included in runtime planner catalogs."""
    monkeypatch.setattr(
        "agent.tools.tool_registry.available_tools",
        lambda: [
            "filesystem.read_file",
            "information_gathering.network_discovery.nmap",
            "shell.utility",
        ],
    )

    catalog = planner_service.get_full_tool_catalog_for_planner(
        SimpleNamespace(max_tools_exposed=10),
        logger=logging.getLogger(__name__),
    )

    assert catalog == [
        "information_gathering.network_discovery.nmap",
        "shell.utility",
    ]


def test_full_planner_catalog_preserves_universal_shell_tools_under_default_cap(monkeypatch) -> None:
    """Universal shell utilities survive the bounded runtime planner catalog."""
    monkeypatch.setattr(
        "agent.tools.tool_registry.available_tools",
        lambda: [
            "information_gathering.network_discovery.nmap",
            "web_applications.web_crawlers.ffuf",
            "shell.utility",
            "shell.assessment",
            "shell.write_stdin",
            "shell.exec",
            "shell.script",
        ],
    )

    catalog = planner_service.get_full_tool_catalog_for_planner(
        SimpleNamespace(max_tools_exposed=3),
        logger=logging.getLogger(__name__),
    )

    assert catalog == ["shell.utility", "shell.assessment", "shell.write_stdin"]
    assert "shell.exec" not in catalog
    assert "shell.script" not in catalog


def test_category_planner_catalog_uses_mvp_allowlist(monkeypatch) -> None:
    """Category-filtered runtime planner catalogs also apply MVP visibility."""
    monkeypatch.setattr(
        "agent.tools.category_utils.get_tools_for_categories",
        lambda _categories: [
            "filesystem.read_file",
            "information_gathering.network_discovery.nmap",
            "shell.utility",
            "shell.assessment",
            "shell.write_stdin",
        ],
    )

    catalog = planner_service.get_category_filtered_catalog(
        ["filesystem"],
        SimpleNamespace(max_tools_exposed=10),
        logger=logging.getLogger(__name__),
        get_full_tool_catalog_for_planner_fn=lambda _config: ["filesystem.read_file"],
    )

    assert catalog == [
        "information_gathering.network_discovery.nmap",
        "shell.utility",
        "shell.assessment",
        "shell.write_stdin",
    ]


def test_category_planner_catalog_uses_visible_shell_aliases(monkeypatch) -> None:
    """Category catalogs expose shell aliases instead of hidden legacy utilities."""
    captured_categories = []

    def fake_get_tools_for_categories(categories):
        captured_categories.extend(categories)
        return [
            "filesystem.read_file",
            "shell.utility",
            "shell.assessment",
            "shell.write_stdin",
            "shell.exec",
            "networking_utilities.network",
            "information_gathering.network_discovery.nmap",
            "information_gathering.osint.whois",
        ]

    monkeypatch.setattr(
        "agent.tools.category_utils.get_tools_for_categories",
        fake_get_tools_for_categories,
    )

    catalog = planner_service.get_category_filtered_catalog(
        ["information_gathering"],
        SimpleNamespace(max_tools_exposed=10),
        logger=logging.getLogger(__name__),
        get_full_tool_catalog_for_planner_fn=lambda _config: ["filesystem.read_file"],
    )

    assert "networking_utilities" in captured_categories
    assert catalog == [
        "shell.utility",
        "shell.assessment",
        "shell.write_stdin",
        "information_gathering.network_discovery.nmap",
    ]
    assert "networking_utilities.network" not in catalog
    assert "shell.exec" not in catalog
    assert "information_gathering.osint.whois" not in catalog
