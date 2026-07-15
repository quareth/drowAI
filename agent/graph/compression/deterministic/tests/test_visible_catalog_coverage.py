"""Coverage tests for visible deterministic compression adapter registration."""

from __future__ import annotations

from agent.graph.compression.deterministic import (
    credential_attack as _credential_attack_registration,
)
from agent.graph.compression.deterministic import (
    filesystem as _filesystem_registration,
)
from agent.graph.compression.deterministic import http as _http_registration
from agent.graph.compression.deterministic import (
    network_discovery as _network_discovery_registration,
)
from agent.graph.compression.deterministic import pcap as _pcap_registration
from agent.graph.compression.deterministic import utility as _utility_registration
from agent.graph.compression.deterministic import (
    web_discovery as _web_discovery_registration,
)
from agent.graph.compression.deterministic.registry import get_adapter
from agent.tools.catalog_visibility import visible_available_tools

_REGISTERED_ADAPTER_MODULES = (
    _credential_attack_registration,
    _filesystem_registration,
    _http_registration,
    _network_discovery_registration,
    _pcap_registration,
    _utility_registration,
    _web_discovery_registration,
)

_VISIBLE_TOOL_EXEMPTIONS: dict[str, str] = {}


def test_visible_tools_have_registered_adapter_or_documented_exemption() -> None:
    """New visible tools must add deterministic coverage or a scoped exemption."""

    assert _REGISTERED_ADAPTER_MODULES
    visible_tools = visible_available_tools()
    assert visible_tools

    uncovered_tools = sorted(
        tool_id
        for tool_id in visible_tools
        if get_adapter(tool_id) is None and tool_id not in _VISIBLE_TOOL_EXEMPTIONS
    )

    assert uncovered_tools == []


def test_visible_tool_exemptions_stay_tied_to_visible_catalog() -> None:
    """The exemption fixture documents only currently visible uncovered tools."""

    visible_tools = set(visible_available_tools())
    undocumented_exemptions = sorted(
        tool_id
        for tool_id, reason in _VISIBLE_TOOL_EXEMPTIONS.items()
        if not reason.strip()
    )
    obsolete_exemptions = sorted(
        tool_id for tool_id in _VISIBLE_TOOL_EXEMPTIONS if tool_id not in visible_tools
    )

    assert undocumented_exemptions == []
    assert obsolete_exemptions == []
