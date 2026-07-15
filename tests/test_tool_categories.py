import os
import sys

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.tools.categories import ToolCategory, PentestPhase


def test_tool_categories() -> None:
    assert len(ToolCategory) >= 15
    assert ToolCategory.NETWORK_DISCOVERY.value == "network_discovery"
    assert all(isinstance(cat.value, str) for cat in ToolCategory)


def test_pentest_phases() -> None:
    assert PentestPhase.RECONNAISSANCE.value == "reconnaissance"
    assert PentestPhase.EXPLOITATION.value == "exploitation"
