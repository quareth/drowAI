"""Unit tests for network-discovery deterministic compression helpers."""

from __future__ import annotations

from agent.graph.compression.deterministic.contracts import CompressionInput
from agent.graph.compression.deterministic.network_discovery import (
    FPING_TOOL_ID,
    MASSCAN_REFERENCE_TOOL_ID,
    NMAP_TOOL_ID,
    network_discovery_adapter,
    registered_network_discovery_tool_ids,
)
from agent.graph.compression.deterministic.registry import (
    compress_deterministically,
    get_adapter,
)
from agent.tools.catalog_visibility import (
    is_tool_hidden_from_catalog,
    visible_available_tools,
)
from agent.tools.information_gathering.network_discovery.masscan import (
    parse_masscan_json,
)
from agent.tools.information_gathering.network_discovery.nmap import parse_nmap_xml


def test_network_discovery_adapter_registers_visible_tools_without_masscan() -> None:
    """Nmap/fping are registered while hidden masscan remains only a reference."""

    assert get_adapter(NMAP_TOOL_ID) is network_discovery_adapter
    assert get_adapter(FPING_TOOL_ID) is network_discovery_adapter
    assert get_adapter(MASSCAN_REFERENCE_TOOL_ID) is None
    assert registered_network_discovery_tool_ids() == (NMAP_TOOL_ID, FPING_TOOL_ID)


def test_nmap_adapter_reuses_masscan_style_host_port_summary_shape() -> None:
    """Nmap parsed hosts and open ports are compacted into grouped summaries."""

    metadata = parse_nmap_xml(
        """
        <nmaprun>
          <host>
            <status state="up"/>
            <address addr="10.0.0.5" addrtype="ipv4"/>
            <hostnames><hostname name="web.local"/></hostnames>
            <ports>
              <port protocol="tcp" portid="80">
                <state state="open"/>
                <service name="http" product="nginx" version="1.24"/>
                <script id="http-title" output="Welcome"/>
                <script id="http-server-header" output="nginx"/>
              </port>
              <port protocol="tcp" portid="443">
                <state state="open"/>
                <service name="https"/>
              </port>
              <port protocol="tcp" portid="22">
                <state state="closed"/>
                <service name="ssh"/>
              </port>
            </ports>
          </host>
          <runstats><hosts up="1" down="1" total="2"/></runstats>
        </nmaprun>
        """
    )

    result = compress_deterministically(
        CompressionInput(
            tool_name=NMAP_TOOL_ID,
            raw_result={
                "metadata": {
                    **metadata,
                    "semantic_evidence": [
                        {
                            "type": "result_summary",
                            "name": "open_ports_count",
                            "value": 2,
                        },
                    ],
                    "semantic_observations": [
                        {
                            "observation_type": "network.service_profiled",
                            "subject_key": "service.socket:10.0.0.5:tcp:80",
                        },
                    ],
                },
                "artifacts": [
                    "artifacts/nmap_123.xml",
                    {
                        "path": "https://objects.local/private/nmap.xml?X-Amz-Signature=secret",
                        "artifact_id": "artifact-1",
                        "artifact_kind": "object_store",
                    },
                ],
            },
        )
    )

    assert result.summary == "Nmap discovered 2 open ports; 1 hosts up, 2 hosts scanned."
    assert result.key_findings == (
        "host 10.0.0.5 (up, names=web.local): 2 open ports - tcp/80 http open (nginx 1.24; title=Welcome; server=nginx), tcp/443 https open",
        "artifact: artifacts/nmap_123.xml",
        "artifact: artifact://artifact-1",
    )
    assert result.decision_evidence == (
        "open port: 10.0.0.5:tcp/80 http open (nginx 1.24)",
        "open port: 10.0.0.5:tcp/443 https open",
        "semantic evidence: open_ports_count=2",
        "semantic observation: network.service_profiled service.socket:10.0.0.5:tcp:80",
    )
    assert result.structured_signals == (
        {"type": "kv_pair", "key": "hosts_total", "value": 2},
        {"type": "kv_pair", "key": "hosts_up", "value": 1},
        {"type": "kv_pair", "key": "hosts_down", "value": 1},
        {"type": "kv_pair", "key": "open_port_count", "value": 2},
        {
            "type": "kv_pair",
            "key": "hostnames:10.0.0.5",
            "value": "web.local",
        },
        {
            "type": "service",
            "target": "10.0.0.5",
            "port": 80,
            "protocol": "tcp",
            "state": "open",
            "service": "http",
            "version": "nginx 1.24",
        },
        {
            "type": "service",
            "target": "10.0.0.5",
            "port": 443,
            "protocol": "tcp",
            "state": "open",
            "service": "https",
        },
        {
            "type": "kv_pair",
            "key": "nmap_artifact_ref",
            "value": "artifacts/nmap_123.xml",
        },
        {
            "type": "kv_pair",
            "key": "nmap_artifact_ref",
            "value": "artifact://artifact-1",
        },
    )
    assert result.completeness == "partial"
    assert result.lossiness_risk == "low"


def test_nmap_parse_error_produces_bounded_canonical_error_summary() -> None:
    """Invalid parsed XML metadata produces explicit compact error context."""

    metadata = parse_nmap_xml("<nmaprun><host>")

    result = compress_deterministically(
        CompressionInput(
            tool_name=NMAP_TOOL_ID,
            raw_result={"metadata": metadata},
        )
    )

    assert result.summary == "Nmap metadata parse failed: Failed to parse XML."
    assert result.errors == ("Failed to parse XML",)
    assert result.structured_signals == (
        {
            "type": "error_context",
            "message": "Nmap metadata parse failed: Failed to parse XML",
        },
    )
    assert result.completeness == "partial"


def test_nmap_empty_hosts_produce_explicit_bounded_summary() -> None:
    """Valid nmap metadata with no hosts does not fall through to no-result."""

    metadata = parse_nmap_xml(
        """
        <nmaprun>
          <runstats><hosts up="0" down="0" total="0"/></runstats>
        </nmaprun>
        """
    )

    result = compress_deterministically(
        CompressionInput(
            tool_name=NMAP_TOOL_ID,
            raw_result={"metadata": metadata},
        )
    )

    assert result.summary == "Nmap discovered 0 open ports; 0 hosts up, 0 hosts scanned."
    assert result.key_findings == ("Nmap metadata contained no hosts and no open ports.",)
    assert result.structured_signals == (
        {"type": "kv_pair", "key": "hosts_total", "value": 0},
        {"type": "kv_pair", "key": "hosts_up", "value": 0},
        {"type": "kv_pair", "key": "hosts_down", "value": 0},
        {"type": "kv_pair", "key": "open_port_count", "value": 0},
    )
    assert result.completeness == "partial"


def test_masscan_parser_shape_stays_reference_only_for_hidden_tool() -> None:
    """Masscan parsed hosts/open_ports are reference shape, not MVP coverage."""

    metadata = parse_masscan_json(
        """
        [
          {
            "ip": "10.0.0.9",
            "timestamp": "2026-06-27T20:00:00Z",
            "ports": [
              {"port": 8080, "proto": "tcp", "status": "open", "service": "http"}
            ]
          }
        ]
        """
    )

    assert metadata["hosts"] == [
        {
            "ip": "10.0.0.9",
            "timestamp": "2026-06-27T20:00:00Z",
            "ports_count": 1,
        }
    ]
    assert metadata["open_ports"] == [
        {"port": 8080, "protocol": "tcp", "status": "open", "service": "http"}
    ]
    assert MASSCAN_REFERENCE_TOOL_ID not in registered_network_discovery_tool_ids()
    assert get_adapter(MASSCAN_REFERENCE_TOOL_ID) is None


def test_hidden_masscan_is_not_required_for_mvp_visible_catalog_coverage() -> None:
    """Coverage follows visible_available_tools(), so hidden masscan is exempt."""

    visible_tools = set(visible_available_tools())

    assert NMAP_TOOL_ID in visible_tools
    assert FPING_TOOL_ID in visible_tools
    assert MASSCAN_REFERENCE_TOOL_ID not in visible_tools
    assert is_tool_hidden_from_catalog(MASSCAN_REFERENCE_TOOL_ID) is True
    assert set(registered_network_discovery_tool_ids()) <= visible_tools


def test_fping_metadata_summary_and_alive_hosts() -> None:
    """Parsed fping metadata is compacted into host-liveness facts."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=FPING_TOOL_ID,
            raw_result={
                "metadata": {
                    "alive_hosts": ["10.0.0.5", "10.0.0.6"],
                    "alive_count": 2,
                    "unresponsive_count": 3,
                    "diagnostics": ["ICMP Host Unreachable from 10.0.0.1"],
                    "exit_code": 1,
                    "semantic_observations": [
                        {
                            "observation_type": "network.host_discovered",
                            "subject_key": "host.ip:10.0.0.5",
                        }
                    ],
                },
                "artifacts": ["artifacts/fping_123.txt"],
            },
        )
    )

    assert result.summary == "fping found 2 alive hosts; 3 unresponsive hosts."
    assert result.key_findings == (
        "alive hosts: 10.0.0.5, 10.0.0.6",
        "unresponsive hosts: 3",
        "diagnostic: ICMP Host Unreachable from 10.0.0.1",
        "artifact: artifacts/fping_123.txt",
    )
    assert result.decision_evidence == (
        "fping liveness: alive=2 unresponsive=3",
        "alive host: 10.0.0.5",
        "alive host: 10.0.0.6",
        "semantic observation: network.host_discovered host.ip:10.0.0.5",
    )
    assert result.structured_signals[:4] == (
        {"type": "kv_pair", "key": "fping_alive_count", "value": 2},
        {"type": "kv_pair", "key": "fping_unresponsive_count", "value": 3},
        {"type": "host", "host": "10.0.0.5", "status": "up"},
        {"type": "host", "host": "10.0.0.6", "status": "up"},
    )


def test_fping_diagnostics_only_has_explicit_zero_alive_summary() -> None:
    """Diagnostic-only fping metadata still gives a useful compact result."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=FPING_TOOL_ID,
            raw_result={
                "metadata": {
                    "alive_hosts": [],
                    "alive_count": 0,
                    "diagnostics": ["ICMP Network Unreachable from 10.0.0.1"],
                    "exit_code": 1,
                }
            },
        )
    )

    assert result.summary == "fping found 0 alive hosts; unresponsive host count unknown."
    assert result.key_findings == (
        "diagnostic: ICMP Network Unreachable from 10.0.0.1",
    )
    assert result.decision_evidence == (
        "fping liveness: alive=0 unresponsive=unknown",
    )
