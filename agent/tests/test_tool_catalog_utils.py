"""Tests for prompt-facing tool catalog assembly utilities."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.graph.utils import tool_catalog
from agent.graph.utils.tool_catalog import build_tool_catalog


class DummyConfig:
    max_tools_exposed = 2


def test_build_tool_catalog_uses_hints():
    metadata = {
        "intent_hints": {"tool_hints": ["network_scan"], "targets": ["127.0.0.1"]},
        "current_phase": "enumeration",
    }
    result = build_tool_catalog(capability=None, metadata=metadata, config=DummyConfig())

    assert isinstance(result.candidates, list)
    assert len(result.candidates) <= DummyConfig.max_tools_exposed
    assert len(result.entries) == len(result.candidates)
    assert all(entry.tool_id for entry in result.entries)
    assert result.hints == ["network_scan"]
    assert result.targets == ["127.0.0.1"]


def test_build_tool_catalog_hides_nikto_and_openvas(monkeypatch: pytest.MonkeyPatch):
    def _fake_resolve_tools_for_capability(_capability, _context, config=None):  # noqa: ANN001
        return [
            "filesystem.grep",
            "information_gathering.network_discovery.nmap",
            "web_applications.web_vulnerability_scanners.nikto",
            "vulnerability_analysis.openvas.openvas",
            "vulnerability_analysis.openvas.greenbone",
        ]

    def _fake_get_tool_metadata(tool_id: str):
        return {"name": tool_id, "category": "test", "description": "test"}

    monkeypatch.setattr(tool_catalog, "resolve_tools_for_capability", _fake_resolve_tools_for_capability)
    monkeypatch.setattr(tool_catalog, "get_tool_metadata", _fake_get_tool_metadata)

    result = build_tool_catalog(
        capability="scan_ports",
        metadata={"intent_hints": {"tool_hints": ["network_scan"]}},
        limit=10,
    )

    assert "information_gathering.network_discovery.nmap" in result.candidates
    assert "filesystem.grep" in result.candidates
    assert "web_applications.web_vulnerability_scanners.nikto" not in result.candidates
    assert "vulnerability_analysis.openvas.openvas" not in result.candidates
    assert "vulnerability_analysis.openvas.greenbone" not in result.candidates


def test_build_tool_catalog_hides_internal_utility_tools(monkeypatch: pytest.MonkeyPatch):
    def _fake_resolve_tools_for_capability(_capability, _context, config=None):  # noqa: ANN001
        _ = config
        return [
            "shell.exec",
            "artifact.search",
            "artifact.read",
            "filesystem.search_text",
        ]

    def _fake_get_tool_metadata(tool_id: str):
        return {"name": tool_id, "category": "test", "description": "test"}

    monkeypatch.setattr(tool_catalog, "resolve_tools_for_capability", _fake_resolve_tools_for_capability)
    monkeypatch.setattr(tool_catalog, "get_tool_metadata", _fake_get_tool_metadata)

    result = build_tool_catalog(
        capability="simple_tool_execution",
        metadata={},
        limit=10,
    )

    assert "shell.exec" not in result.candidates
    assert "filesystem.search_text" in result.candidates
    assert "artifact.search" not in result.candidates
    assert "artifact.read" not in result.candidates


@pytest.mark.parametrize(
    ("hint", "expected_capability", "expected_candidates"),
    [
        ("http_probe", "http_request", ["information_gathering.web_enumeration.http_request"]),
        ("http_fetch", "http", [
            "information_gathering.web_enumeration.http_request",
            "information_gathering.web_enumeration.http_download",
        ]),
        ("file_download", "http_download", ["information_gathering.web_enumeration.http_download"]),
        ("curl", "curl", [
            "information_gathering.web_enumeration.http_request",
            "information_gathering.web_enumeration.http_download",
        ]),
        ("http_trace", "http_debug", [
            "information_gathering.web_enumeration.http_request",
            "information_gathering.web_enumeration.http_download",
        ]),
        ("multipart_upload", "http_upload", ["information_gathering.web_enumeration.http_request"]),
        ("mtls", "http_tls", [
            "information_gathering.web_enumeration.http_request",
            "information_gathering.web_enumeration.http_download",
        ]),
        ("http3", "http_protocol", [
            "information_gathering.web_enumeration.http_request",
            "information_gathering.web_enumeration.http_download",
        ]),
    ],
)
def test_build_tool_catalog_maps_http_hints(monkeypatch: pytest.MonkeyPatch, hint, expected_capability, expected_candidates):
    seen_capabilities = []

    def _fake_resolve_tools_for_capability(capability, _context, config=None):  # noqa: ANN001
        _ = config
        seen_capabilities.append(capability)
        if capability == "http_request":
            return ["information_gathering.web_enumeration.http_request"]
        if capability == "http_download":
            return ["information_gathering.web_enumeration.http_download"]
        if capability in {"http", "curl"}:
            return [
                "information_gathering.web_enumeration.http_request",
                "information_gathering.web_enumeration.http_download",
            ]
        if capability in {"http_debug", "http_tls", "http_protocol"}:
            return [
                "information_gathering.web_enumeration.http_request",
                "information_gathering.web_enumeration.http_download",
            ]
        if capability == "http_upload":
            return ["information_gathering.web_enumeration.http_request"]
        return []

    def _fake_get_tool_metadata(tool_id: str):
        return {"name": tool_id, "category": "web_enumeration", "description": "test"}

    monkeypatch.setattr(tool_catalog, "resolve_tools_for_capability", _fake_resolve_tools_for_capability)
    monkeypatch.setattr(tool_catalog, "get_tool_metadata", _fake_get_tool_metadata)

    result = build_tool_catalog(
        capability=None,
        metadata={"intent_hints": {"tool_hints": [hint]}},
        limit=5,
    )

    assert seen_capabilities
    assert seen_capabilities[0] == expected_capability
    assert result.candidates == expected_candidates


def test_build_tool_catalog_fallback_resolver_prevents_empty_on_resolver_failure(monkeypatch: pytest.MonkeyPatch):
    def _broken_resolver(_capability, _context, config=None):  # noqa: ANN001
        _ = config
        raise RuntimeError("resolver unavailable")

    monkeypatch.setattr(tool_catalog, "resolve_tools_for_capability", _broken_resolver)
    monkeypatch.setattr(
        tool_catalog,
        "available_tools_fn",
        lambda: [
            "information_gathering.web_enumeration.http_request",
            "information_gathering.web_enumeration.http_download",
        ],
    )

    result = build_tool_catalog(
        capability=None,
        metadata={"intent_hints": {"tool_hints": ["http_probe"]}},
        limit=5,
    )

    assert result.candidates == ["information_gathering.web_enumeration.http_request"]


def test_build_tool_catalog_fails_closed_when_visibility_filter_fails(monkeypatch: pytest.MonkeyPatch):
    def _resolver_with_hidden(_capability, _context, config=None):  # noqa: ANN001
        _ = config
        return [
            "information_gathering.network_discovery.nmap",
            "web_applications.web_vulnerability_scanners.nikto",
            "vulnerability_analysis.openvas.openvas",
        ]

    def _broken_visibility_filter(_tool_ids):  # noqa: ANN001
        raise RuntimeError("visibility dependency unavailable")

    monkeypatch.setattr(tool_catalog, "resolve_tools_for_capability", _resolver_with_hidden)
    monkeypatch.setattr(tool_catalog, "filter_visible_tool_ids", _broken_visibility_filter)
    monkeypatch.setattr(
        tool_catalog,
        "get_tool_metadata",
        lambda tool_id: {"name": tool_id, "category": "test", "description": "test"},
    )

    result = build_tool_catalog(
        capability="scan_ports",
        metadata={"intent_hints": {"tool_hints": ["network_scan"]}},
        limit=10,
    )

    assert result.candidates == []
    assert result.entries == []
    assert "web_applications.web_vulnerability_scanners.nikto" not in result.candidates
    assert "vulnerability_analysis.openvas.openvas" not in result.candidates
