"""Unit tests for network utility deterministic compression helpers."""

from __future__ import annotations

from agent.graph.compression.deterministic.contracts import CompressionInput
from agent.graph.compression.deterministic.registry import (
    compress_deterministically,
    get_adapter,
)
from agent.graph.compression.deterministic.utility import (
    NETWORK_UTILITY_TOOL_ID,
    registered_utility_tool_ids,
    utility_adapter,
)


def test_utility_adapter_registers_visible_utility_tool_ids() -> None:
    """Visible utility tools resolve to the deterministic utility adapter."""

    assert registered_utility_tool_ids() == (NETWORK_UTILITY_TOOL_ID,)
    assert get_adapter(NETWORK_UTILITY_TOOL_ID) is utility_adapter


def test_network_utility_tcp_connect_surfaces_reachability() -> None:
    """Network utility metadata yields operation, target, and reachability facts."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=NETWORK_UTILITY_TOOL_ID,
            raw_result={
                "success": True,
                "parameters": {
                    "operation": "tcp_connect",
                    "target": "10.0.0.5",
                    "port": 22,
                },
                "metadata": {
                    "operation": "tcp_connect",
                    "target": "10.0.0.5",
                    "port": 22,
                    "success": True,
                    "reachable": True,
                    "stdout_preview": "",
                    "stderr_preview": "Connection to 10.0.0.5 22 port [tcp/ssh] succeeded!",
                    "stdout_line_count": 0,
                },
            },
        )
    )

    assert result.summary == "Network utility tcp_connect against 10.0.0.5:22: reachable."
    assert result.completeness == "partial"
    assert "reachability: 10.0.0.5:22 reachable" in result.key_findings
    assert "stderr preview: Connection to 10.0.0.5 22 port [tcp/ssh] succeeded!" in result.decision_evidence
    assert result.structured_signals[:4] == (
        {"type": "kv_pair", "key": "network_utility_outcome", "value": "reachable"},
        {"type": "kv_pair", "key": "network_utility_operation", "value": "tcp_connect"},
        {"type": "kv_pair", "key": "network_utility_target", "value": "10.0.0.5:22"},
        {"type": "service", "target": "10.0.0.5:22", "state": "reachable"},
    )


def test_network_utility_dns_empty_output_is_explicit() -> None:
    """Empty DNS metadata still reports the requested operation and target."""

    result = compress_deterministically(
        CompressionInput(
            tool_name=NETWORK_UTILITY_TOOL_ID,
            raw_result={
                "success": True,
                "parameters": {
                    "operation": "dns_lookup",
                    "target": "example.invalid",
                    "record_type": "TXT",
                },
                "metadata": {
                    "operation": "dns_lookup",
                    "target": "example.invalid",
                    "record_type": "TXT",
                    "success": True,
                    "answer_count": 0,
                    "stdout_line_count": 0,
                },
            },
        )
    )

    assert result.summary == "Network utility dns_lookup against example.invalid: success."
    assert "record type: TXT" in result.key_findings
    assert "answers: 0" in result.key_findings
