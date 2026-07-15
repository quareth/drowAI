import pytest

import os
import sys

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.tools.action_mapper import ContextualToolSelector
from agent.models import ActionType


def test_action_tool_mapping_returns_web_tools():
    selector = ContextualToolSelector()
    context = {"discovered_services": {"http": {"port": 80}}}
    tools = selector.select_tools_for_action(ActionType.SCAN_WEB, context)
    assert any("gobuster" in t for t in tools)


def test_service_filter_excludes_mismatched_services():
    selector = ContextualToolSelector()
    context = {"discovered_services": {"http": {"port": 80}}}
    tools = selector.select_tools_for_action(ActionType.ENUMERATE_SERVICES, context)
    assert all("dnsrecon" not in t for t in tools)


def test_phase_filtering_removes_non_phase_tools():
    selector = ContextualToolSelector()
    context = {"current_phase": "reconnaissance"}
    tools = selector.select_tools_for_action(ActionType.SCAN_WEB, context)
    assert all("gobuster" not in t for t in tools)


def test_prioritization_orders_by_metadata():
    selector = ContextualToolSelector()
    context = {"discovered_services": {"dns": {"port": 53}}}
    tools = selector.select_tools_for_action(ActionType.ENUMERATE_SERVICES, context)
    nmap_id = "information_gathering.network_discovery.nmap"
    dnsrecon_id = "information_gathering.dns.dnsrecon"
    assert nmap_id in tools and dnsrecon_id in tools
    assert tools.index(nmap_id) < tools.index(dnsrecon_id)


def test_max_tools_per_action_respected():
    selector = ContextualToolSelector()
    # Provide a high-signal context but cap selection to 1
    context = {
        "discovered_services": {"http": {"port": 80}},
        "max_tools_per_action": 1,
    }
    tools = selector.select_tools_for_action(ActionType.SCAN_WEB, context)
    assert len(tools) == 1


def test_phase_filtering_includes_enumeration_web_tools():
    selector = ContextualToolSelector()
    context = {"current_phase": "enumeration"}
    tools = selector.select_tools_for_action(ActionType.SCAN_WEB, context)
    # In enumeration, gobuster is phase-appropriate
    assert any("gobuster" in t for t in tools)
