import os
import sys

import pytest

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.tools.compatibility import ToolCompatibilityAnalyzer


def test_tool_compatibility_matrix():
    analyzer = ToolCompatibilityAnalyzer()
    nmap = "information_gathering.network_discovery.nmap"
    gobuster = "web_applications.web_crawlers.gobuster"
    metasploit = "exploitation_tools.metasploit.run_exploit"
    # Prefer primary sqlmap location used in action map
    sqlmap = "web_applications.web_vulnerability_scanners.sqlmap"

    assert analyzer.can_run_together(nmap, gobuster)
    assert not analyzer.can_run_together(metasploit, sqlmap)


def test_grouping_separates_exclusive_tools():
    analyzer = ToolCompatibilityAnalyzer()
    nmap = "information_gathering.network_discovery.nmap"
    gobuster = "web_applications.web_crawlers.gobuster"
    metasploit = "exploitation_tools.metasploit.run_exploit"

    groups = analyzer.group_compatible_tools([nmap, gobuster, metasploit])
    assert len(groups) == 2
    assert any(nmap in g and gobuster in g for g in groups)
    assert any(metasploit in g and len(g) == 1 for g in groups)
