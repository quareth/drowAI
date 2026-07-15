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
            "filesystem.grep",
            "filesystem.search_text",
        ],
    )

    catalog = planner_service.get_full_tool_catalog_for_planner(
        SimpleNamespace(max_tools_exposed=10),
        logger=logging.getLogger(__name__),
    )

    assert catalog == ["filesystem.read_file", "filesystem.grep", "filesystem.search_text"]


def test_category_planner_catalog_uses_mvp_allowlist(monkeypatch) -> None:
    """Category-filtered runtime planner catalogs also apply MVP visibility."""
    monkeypatch.setattr(
        "agent.tools.category_utils.get_tools_for_categories",
        lambda _categories: [
            "filesystem.read_file",
            "filesystem.grep",
            "filesystem.search_text",
        ],
    )

    catalog = planner_service.get_category_filtered_catalog(
        ["filesystem"],
        SimpleNamespace(max_tools_exposed=10),
        logger=logging.getLogger(__name__),
        get_full_tool_catalog_for_planner_fn=lambda _config: ["filesystem.read_file"],
    )

    assert catalog == ["filesystem.read_file", "filesystem.grep", "filesystem.search_text"]


def test_category_planner_catalog_includes_networking_utilities(monkeypatch) -> None:
    """Network utilities are always available in category-filtered catalogs."""
    captured_categories = []

    def fake_get_tools_for_categories(categories):
        captured_categories.extend(categories)
        return [
            "filesystem.read_file",
            "shell.exec",
            "networking_utilities.network",
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
    assert "networking_utilities.network" in catalog
    assert "shell.exec" not in catalog
    assert "information_gathering.osint.whois" not in catalog
