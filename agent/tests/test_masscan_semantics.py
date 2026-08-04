"""Masscan semantic fact emission tests.

This module locks producer-owned canonical host, open-port, and concrete-service
facts emitted from Masscan's already parsed metadata.
"""

from __future__ import annotations

from agent.tools.information_gathering.network_discovery.masscan import (
    MasscanArgs,
    MasscanTool,
    parse_masscan_json,
)
from runtime_shared.semantic.pentest_facts import SemanticFactEnvelope, compile_facts


def _emit(metadata: dict) -> list[dict]:
    return MasscanTool().emit_semantic_observations(
        stdout="not parsed by semantic emitter",
        stderr="",
        exit_code=0,
        args=MasscanArgs(target="192.0.2.20", ports="443"),
        metadata=metadata,
    )


def test_masscan_emits_host_port_and_concrete_service_facts_from_metadata() -> None:
    observations = _emit(
        {
            "hosts": [{"ip": "192.0.2.20"}],
            "open_ports": [
                {
                    "port": 443,
                    "protocol": "tcp",
                    "service": "https",
                }
            ],
        }
    )
    compiled = compile_facts(
        SemanticFactEnvelope(
            semantic_schema_version="masscan.v1",
            capability_family="network_discovery",
            observations=tuple(observations),
            evidence=(),
        )
    )

    assert observations == [
        {
            "observation_type": "network.host_discovered",
            "subject_type": "host.ip",
            "subject_key": "host.ip:192.0.2.20",
            "payload": {"source": "masscan"},
        },
        {
            "observation_type": "network.open_port",
            "subject_type": "service.socket",
            "subject_key": "service.socket:192.0.2.20/tcp/443",
            "payload": {
                "ip": "192.0.2.20",
                "protocol": "tcp",
                "port": 443,
                "source": "masscan",
            },
        },
        {
            "observation_type": "network.service_detected",
            "subject_type": "service.socket",
            "subject_key": "service.socket:192.0.2.20/tcp/443",
            "payload": {"service_name": "https", "source": "masscan"},
        },
    ]
    assert compiled.accepted_count == 3
    assert compiled.rejected_count == 0
    assert compiled.duplicate_count == 0
    assert compiled.diagnostics == ()


def test_masscan_parses_multi_host_port_identity_for_tcp_and_udp_semantics() -> None:
    stdout = """
    [
      {"ip": "192.0.2.20", "timestamp": "1700000000", "ports": [
        {"port": 443, "proto": "tcp", "status": "open", "service": "https"}
      ]},
      {"ip": "198.51.100.7", "timestamp": "1700000001", "ports": [
        {"port": 53, "proto": "udp", "status": "open", "service": "dns"}
      ]}
    ]
    """

    metadata = parse_masscan_json(stdout)
    observations = MasscanTool().emit_semantic_observations(
        stdout="not parsed by semantic emitter",
        stderr="",
        exit_code=0,
        args=MasscanArgs(target="192.0.2.20,198.51.100.7", ports="53,443"),
        metadata=metadata,
    )
    compiled = compile_facts(
        SemanticFactEnvelope(
            semantic_schema_version="masscan.v1",
            capability_family="network_discovery",
            observations=tuple(observations),
            evidence=(),
        )
    )

    assert metadata["open_ports"] == [
        {
            "ip": "192.0.2.20",
            "port": 443,
            "protocol": "tcp",
            "status": "open",
            "service": "https",
        },
        {
            "ip": "198.51.100.7",
            "port": 53,
            "protocol": "udp",
            "status": "open",
            "service": "dns",
        },
    ]
    assert [item["observation_type"] for item in observations] == [
        "network.host_discovered",
        "network.host_discovered",
        "network.open_port",
        "network.service_detected",
        "network.open_port",
        "network.service_detected",
    ]
    assert [item["subject_key"] for item in observations] == [
        "host.ip:192.0.2.20",
        "host.ip:198.51.100.7",
        "service.socket:192.0.2.20/tcp/443",
        "service.socket:192.0.2.20/tcp/443",
        "service.socket:198.51.100.7/udp/53",
        "service.socket:198.51.100.7/udp/53",
    ]
    assert compiled.accepted_count == 6
    assert compiled.rejected_count == 0
    assert compiled.duplicate_count == 0
    assert compiled.diagnostics == ()


def test_masscan_semantics_skip_explicit_non_open_port_rows() -> None:
    stdout = """
    [
      {"ip": "192.0.2.20", "timestamp": "1700000000", "ports": [
        {"port": 22, "proto": "tcp", "status": "closed", "service": "ssh"},
        {"port": 53, "proto": "udp", "status": "filtered", "service": "dns"},
        {"port": 80, "proto": "tcp", "status": " OPEN ", "service": "http"},
        {"port": 443, "proto": "tcp", "service": "https"}
      ]}
    ]
    """

    metadata = parse_masscan_json(stdout)
    observations = _emit(metadata)
    compiled = compile_facts(
        SemanticFactEnvelope(
            semantic_schema_version="masscan.v1",
            capability_family="network_discovery",
            observations=tuple(observations),
            evidence=(),
        )
    )

    assert [
        (item["observation_type"], item["subject_key"])
        for item in observations
    ] == [
        ("network.host_discovered", "host.ip:192.0.2.20"),
        ("network.open_port", "service.socket:192.0.2.20/tcp/80"),
        ("network.service_detected", "service.socket:192.0.2.20/tcp/80"),
        ("network.open_port", "service.socket:192.0.2.20/tcp/443"),
        ("network.service_detected", "service.socket:192.0.2.20/tcp/443"),
    ]
    assert compiled.accepted_count == 5
    assert compiled.rejected_count == 0


def test_masscan_semantics_keep_empty_and_partial_scan_behavior() -> None:
    assert _emit({}) == []
    assert _emit({"hosts": [{"ip": "192.0.2.20"}], "open_ports": []}) == [
        {
            "observation_type": "network.host_discovered",
            "subject_type": "host.ip",
            "subject_key": "host.ip:192.0.2.20",
            "payload": {"source": "masscan"},
        }
    ]
    assert _emit(
        {
            "hosts": [{"ip": "192.0.2.20"}, {"ip": "198.51.100.7"}],
            "open_ports": [{"port": 443, "protocol": "tcp", "service": "https"}],
        }
    ) == [
        {
            "observation_type": "network.host_discovered",
            "subject_type": "host.ip",
            "subject_key": "host.ip:192.0.2.20",
            "payload": {"source": "masscan"},
        },
        {
            "observation_type": "network.host_discovered",
            "subject_type": "host.ip",
            "subject_key": "host.ip:198.51.100.7",
            "payload": {"source": "masscan"},
        },
    ]


def test_masscan_semantics_skip_invalid_rows_and_dedupe_exact_observations() -> None:
    observations = _emit(
        {
            "hosts": [
                {"ip": "192.0.2.20"},
                {"ip": "192.0.2.20"},
                {"ip": "not an ip"},
            ],
            "open_ports": [
                {"port": "443", "protocol": "tcp", "service": "HTTPS"},
                {"port": "443", "protocol": "tcp", "service": "HTTPS"},
                {"ip": "192.0.2.21", "port": "53", "protocol": "udp", "service": "?"},
                {"ip": "not an ip", "port": 22, "protocol": "tcp", "service": "ssh"},
                {"ip": "192.0.2.22", "port": 0, "protocol": "tcp", "service": "ssh"},
                {"ip": "192.0.2.23", "port": 22, "protocol": "icmp", "service": "ssh"},
            ],
        }
    )
    compiled = compile_facts(
        SemanticFactEnvelope(
            semantic_schema_version="masscan.v1",
            capability_family="network_discovery",
            observations=tuple(observations),
            evidence=(),
        )
    )

    assert [item["subject_key"] for item in observations] == [
        "host.ip:192.0.2.20",
        "service.socket:192.0.2.20/tcp/443",
        "service.socket:192.0.2.20/tcp/443",
        "service.socket:192.0.2.21/udp/53",
    ]
    assert [item["observation_type"] for item in observations] == [
        "network.host_discovered",
        "network.open_port",
        "network.service_detected",
        "network.open_port",
    ]
    assert compiled.accepted_count == len(observations)
    assert compiled.rejected_count == 0
    assert compiled.duplicate_count == 0
    assert compiled.diagnostics == ()
