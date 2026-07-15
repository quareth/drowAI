"""Tool-level tests for the finite grouped network utility tool."""

from __future__ import annotations

import subprocess

import pytest
from pydantic import ValidationError

from agent.tools.parameter_validation import validate_tool_parameters
from agent.tools.networking_utilities.network import (
    NetworkUtilityArgs,
    NetworkUtilityOperation,
    NetworkUtilityTool,
)


def _collect_schema_defaults(node):
    defaults = []
    if isinstance(node, dict):
        if "default" in node:
            defaults.append(node["default"])
        for value in node.values():
            defaults.extend(_collect_schema_defaults(value))
    elif isinstance(node, list):
        for value in node:
            defaults.extend(_collect_schema_defaults(value))
    return defaults


def test_network_utility_planner_schema_exposes_no_defaults():
    schema = NetworkUtilityTool.get_planner_args_model().model_json_schema()

    assert _collect_schema_defaults(schema) == []


def test_network_utility_planner_schema_mandates_finite_operation_controls():
    schema = NetworkUtilityTool.get_planner_args_model().model_json_schema()
    conditional_requirements = {}
    for rule in schema.get("allOf", []):
        operation = (rule.get("if", {}).get("properties", {}).get("operation", {}).get("const"))
        if operation:
            conditional_requirements[operation] = set(rule.get("then", {}).get("required", []))

    assert conditional_requirements["ping"] >= {
        "operation",
        "target",
        "count",
        "timeout_sec",
        "per_probe_timeout_sec",
    }
    assert conditional_requirements["dns_lookup"] >= {
        "operation",
        "target",
        "record_type",
        "timeout_sec",
        "per_probe_timeout_sec",
    }
    assert conditional_requirements["whois"] >= {"operation", "target", "timeout_sec"}
    assert conditional_requirements["tcp_connect"] >= {
        "operation",
        "target",
        "port",
        "timeout_sec",
        "per_probe_timeout_sec",
    }
    assert conditional_requirements["trace_route"] >= {
        "operation",
        "target",
        "timeout_sec",
        "per_probe_timeout_sec",
        "max_hops",
        "queries",
    }
    assert conditional_requirements["local_interfaces"] >= {"operation", "timeout_sec"}
    assert conditional_requirements["local_routes"] >= {"operation", "timeout_sec"}
    assert conditional_requirements["local_neighbors"] >= {"operation", "timeout_sec"}


def test_network_utility_planner_accepts_local_interfaces_with_llm_chosen_timeout():
    result = validate_tool_parameters(
        "networking_utilities.network",
        {"operation": "local_interfaces", "timeout_sec": 5},
        validation_stage="planner",
    )

    assert result.valid is True
    assert result.normalized_parameters == {
        "operation": "local_interfaces",
        "timeout_sec": 5,
    }


def test_network_utility_schema_requires_timeout_for_local_operations():
    with pytest.raises(ValidationError):
        NetworkUtilityArgs(operation="local_interfaces")


def test_network_utility_planner_rejects_irrelevant_local_interface_values():
    result = validate_tool_parameters(
        "networking_utilities.network",
        {
            "operation": "local_interfaces",
            "timeout_sec": 5,
            "count": 4,
            "max_hops": 30,
            "queries": 1,
        },
        validation_stage="planner",
    )

    assert result.valid is False
    assert result.reason == "semantic_validation_error"


def test_network_utility_schema_requires_targets_for_remote_operations():
    for operation in [
        "ping",
        "dns_lookup",
        "whois",
        "tcp_connect",
        "trace_route",
    ]:
        payload = {"operation": operation}
        if operation == "tcp_connect":
            payload["port"] = 443
        with pytest.raises(ValidationError):
            NetworkUtilityArgs(**payload)


def test_network_utility_schema_rejects_target_for_local_operations():
    with pytest.raises(ValidationError):
        NetworkUtilityArgs(operation="local_interfaces", target="127.0.0.1", timeout_sec=5)


def test_network_utility_schema_requires_tcp_connect_port():
    with pytest.raises(ValidationError):
        NetworkUtilityArgs(operation="tcp_connect", target="127.0.0.1")


def test_network_utility_schema_rejects_extra_and_wrong_operation_fields():
    with pytest.raises(ValidationError):
        NetworkUtilityArgs(operation="ping", target="127.0.0.1", raw_command="id")
    with pytest.raises(ValidationError):
        NetworkUtilityArgs(operation="whois", target="example.com", resolver="1.1.1.1")
    with pytest.raises(ValidationError):
        NetworkUtilityArgs(operation="dns_lookup", target="-example.com")


def test_network_utility_ping_command_is_finite():
    tool = NetworkUtilityTool()
    args = NetworkUtilityArgs(
        operation=NetworkUtilityOperation.PING,
        target="127.0.0.1",
        count=3,
        timeout_sec=9,
        per_probe_timeout_sec=2,
    )

    cmd = tool.build_command(args)

    assert cmd[:2] == ["ping", "-c"]
    assert cmd[2] == "3"
    assert "-W" in cmd
    assert cmd[cmd.index("-W") + 1] == "2"
    assert "-w" in cmd
    assert cmd[-1] == "127.0.0.1"


def test_network_utility_dig_command_is_finite():
    tool = NetworkUtilityTool()
    args = NetworkUtilityArgs(
        operation="dns_lookup",
        target="example.com",
        record_type="AAAA",
        resolver="1.1.1.1",
        timeout_sec=15,
        per_probe_timeout_sec=3,
    )

    cmd = tool.build_command(args)

    assert cmd == ["dig", "@1.1.1.1", "example.com", "AAAA", "+time=3", "+tries=1"]


def test_network_utility_tcp_connect_command_is_finite():
    tool = NetworkUtilityTool()
    args = NetworkUtilityArgs(
        operation="tcp_connect",
        target="127.0.0.1",
        port=22,
        timeout_sec=15,
        per_probe_timeout_sec=4,
    )

    cmd = tool.build_command(args)

    assert cmd == ["nc", "-vz", "-w", "4", "127.0.0.1", "22"]


def test_network_utility_trace_route_command_is_finite():
    tool = NetworkUtilityTool()
    args = NetworkUtilityArgs(
        operation="trace_route",
        target="127.0.0.1",
        max_hops=12,
        queries=2,
        timeout_sec=15,
        per_probe_timeout_sec=3,
    )

    cmd = tool.build_command(args)

    assert cmd == ["traceroute", "-m", "12", "-q", "2", "-w", "3", "127.0.0.1"]


def test_network_utility_timeout_returns_controlled_metadata(monkeypatch):
    tool = NetworkUtilityTool()
    args = NetworkUtilityArgs(operation="whois", target="example.com", timeout_sec=1)

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["whois", "example.com"], timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = tool.run(args)

    assert result.success is False
    assert result.exit_code == -2
    assert result.metadata["timed_out"] is True
    assert result.metadata["command"] == ["whois", "example.com"]


def test_network_utility_run_metadata_preview(monkeypatch):
    tool = NetworkUtilityTool()
    args = NetworkUtilityArgs(operation="local_routes", timeout_sec=5)

    def fake_run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, "default via 10.0.0.1\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = tool.run(args)

    assert result.success is True
    assert result.metadata["operation"] == "local_routes"
    assert result.metadata["entry_count"] == 1
    assert result.metadata["stdout_preview"] == "default via 10.0.0.1\n"
