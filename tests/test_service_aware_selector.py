import os
import sys

# Add project root to Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent.tools.service_matcher import ServiceAwareSelector, ServiceInfo


def test_http_vulnerability_scanning_uses_zaproxy_id():
    selector = ServiceAwareSelector()
    tools = selector.get_tools_for_service("http", "vulnerability_scanning")
    assert "web_applications.web_application_proxies.zaproxy" in tools


def test_https_vulnerability_scanning_uses_zaproxy_id():
    selector = ServiceAwareSelector()
    tools = selector.get_tools_for_service("https", "vulnerability_scanning")
    assert "web_applications.web_application_proxies.zaproxy" in tools


def test_wordpress_technology_adds_wpscan():
    selector = ServiceAwareSelector()
    svc = ServiceInfo(name="http", port=80, technology="WordPress 6.0")
    tools = selector.get_tools_for_service("http", "enumeration", svc)
    assert any("wpscan" in t for t in tools)


def test_unknown_service_defaults_to_tcp_tools():
    selector = ServiceAwareSelector()
    tools = selector.get_tools_for_service("rmi", "enumeration")
    # Should fallback to tcp enumeration tools
    assert any("network_discovery.nmap" in t for t in tools)


