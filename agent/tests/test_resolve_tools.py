"""Tests for capability-to-tool resolution helpers."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agent.graph.infrastructure.state_models import CapabilityType
from agent.tools.resolve_tools import resolve_tools_for_capability


class DummyConfig:
    max_tools_exposed = 2


def test_resolve_tools_for_scan_ports():
    context = {"current_phase": "enumeration"}
    out = resolve_tools_for_capability(CapabilityType.PORT_SCAN, context, DummyConfig())
    assert isinstance(out, list)
    assert 0 < len(out) <= 2


def test_resolve_tools_alias_handling():
    out = resolve_tools_for_capability("ports", {}, DummyConfig())
    # May be empty if mapping finds nothing; ensure it returns a list
    assert isinstance(out, list)


def test_resolve_tools_uses_canonical_exploitation_category():
    out = resolve_tools_for_capability(CapabilityType.VULN_EXPLOIT, {}, DummyConfig())
    assert isinstance(out, list)
    assert all(not tool.startswith("exploitation.") for tool in out)


def test_resolve_tools_http_aliases(monkeypatch):
    monkeypatch.setattr(
        "agent.tools.resolve_tools.available_tools",
        lambda: [
            "information_gathering.web_enumeration.http_request",
            "information_gathering.web_enumeration.http_download",
        ],
    )

    out_http = resolve_tools_for_capability("http", {}, DummyConfig())
    out_fetch = resolve_tools_for_capability("http_fetch", {}, DummyConfig())
    out_probe = resolve_tools_for_capability("http_probe", {}, DummyConfig())
    out_request = resolve_tools_for_capability("http_request", {}, DummyConfig())
    out_download_alias = resolve_tools_for_capability("http_download", {}, DummyConfig())
    out_download = resolve_tools_for_capability("download", {}, DummyConfig())
    out_file_download = resolve_tools_for_capability("file_download", {}, DummyConfig())
    out_curl = resolve_tools_for_capability("curl", {}, DummyConfig())
    out_http_debug = resolve_tools_for_capability("http_debug", {}, DummyConfig())
    out_http_upload = resolve_tools_for_capability("http_upload", {}, DummyConfig())
    out_http_tls = resolve_tools_for_capability("http_tls", {}, DummyConfig())
    out_http_binary = resolve_tools_for_capability("http_binary", {}, DummyConfig())
    out_http_protocol = resolve_tools_for_capability("http_protocol", {}, DummyConfig())
    out_http2 = resolve_tools_for_capability("http2", {}, DummyConfig())
    out_http3 = resolve_tools_for_capability("http3", {}, DummyConfig())

    assert out_http == [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ]
    assert out_fetch == ["information_gathering.web_enumeration.http_request"]
    assert out_probe == ["information_gathering.web_enumeration.http_request"]
    assert out_request == ["information_gathering.web_enumeration.http_request"]
    assert out_download_alias == ["information_gathering.web_enumeration.http_download"]
    assert out_download == ["information_gathering.web_enumeration.http_download"]
    assert out_file_download == ["information_gathering.web_enumeration.http_download"]
    assert out_curl == [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ]
    assert out_http_debug == [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ]
    assert out_http_upload == ["information_gathering.web_enumeration.http_request"]
    assert out_http_tls == [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ]
    assert out_http_binary == ["information_gathering.web_enumeration.http_request"]
    assert out_http_protocol == [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ]
    assert out_http2 == [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ]
    assert out_http3 == [
        "information_gathering.web_enumeration.http_request",
        "information_gathering.web_enumeration.http_download",
    ]


def test_resolve_tools_network_utility_aliases(monkeypatch):
    monkeypatch.setattr(
        "agent.tools.resolve_tools.available_tools",
        lambda: ["networking_utilities.network"],
    )

    for alias in [
        "network_utility",
        "ping",
        "dig",
        "dns_lookup",
        "whois",
        "tcp_connect",
        "traceroute",
        "interfaces",
        "routes",
    ]:
        assert resolve_tools_for_capability(alias, {}, DummyConfig()) == [
            "networking_utilities.network"
        ]
