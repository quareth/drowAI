import os
import sys

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.tools.service_matcher import (
    ServiceAwareSelector,
    ServiceInfo,
    ServiceInventory,
)


def test_service_inventory_add_and_lookup():
    inventory = ServiceInventory()
    svc = ServiceInfo(name="http", port=80)
    inventory.add_service(svc)
    services = inventory.get_services_by_name("http")
    assert svc in services


def test_http_selection_includes_gobuster():
    selector = ServiceAwareSelector()
    tools = selector.get_tools_for_service("http", "enumeration")
    assert "web_applications.web_crawlers.gobuster" in tools


def test_wordpress_detection_via_version():
    selector = ServiceAwareSelector()
    info = ServiceInfo(name="http", port=80, version="WordPress 6.0")
    tools = selector.get_tools_for_service("http", "enumeration", info)
    assert "web_applications.cms_identification.wpscan" in tools


def test_unknown_service_defaults_to_generic():
    selector = ServiceAwareSelector()
    tools = selector.get_tools_for_service("smtp", "enumeration")
    assert "information_gathering.network_discovery.nmap" in tools
