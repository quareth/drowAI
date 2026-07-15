"""Regression tests for tcpdump's transport-agnostic finite capture contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent.execution_strategy import ExecutionStrategy
from agent.tool_runtime.batch.compatibility import (
    BatchCompatibilityChecker,
    CompatibilityOutcome,
)
from agent.tool_runtime.batch.types import ToolBatch, ToolCall
from agent.tool_runtime.timeout_policy import resolve_tool_timeout_plan
from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata
from agent.tools.sniffing_spoofing.network_sniffers.tcpdump import (
    TCPDUMP_DEFAULT_SNAPLEN,
    TCPDUMP_HARD_TIMEOUT_SECONDS,
    TCPDUMP_PACKET_LIMIT,
    TCPDUMP_TIMEOUT_EXIT_CODE,
    TcpdumpArgs,
    TcpdumpPlannerArgs,
    TcpdumpTool,
)
from tests.tools.validation.command_validator import validate_tool_command

TCPDUMP_TOOL_ID = "sniffing_spoofing.network_sniffers.tcpdump"


def _flag_value(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def test_tcpdump_planner_schema_hides_runtime_controls() -> None:
    schema = TcpdumpTool.get_planner_args_model().model_json_schema()
    properties = set(schema.get("properties", {}))

    assert {
        "interface",
        "protocol",
        "host",
        "port",
        "bpf_filter",
        "verbose_level",
        "quiet",
        "include_payload",
    }.issubset(properties)
    assert {
        "target",
        "timeout",
        "timeout_seconds",
        "packet_count",
        "duration_seconds",
        "snaplen",
        "write_file",
    }.isdisjoint(properties)

    include_payload_description = (
        schema["properties"]["include_payload"].get("description") or ""
    )
    assert "PCAP" in include_payload_description
    assert "HTTPS/TLS" in include_payload_description

    with pytest.raises(ValidationError):
        TcpdumpPlannerArgs(timeout=1)  # type: ignore[call-arg]


def test_tcpdump_planner_compilation_preserves_semantic_filters_only() -> None:
    compiled = TcpdumpTool.compile_planner_parameters(
        {
            "interface": "eth0",
            "protocol": "tcp",
            "host": "192.0.2.10",
            "port": 443,
            "bpf_filter": "tcp[tcpflags] & tcp-syn != 0",
            "quiet": True,
            "include_payload": True,
        },
        action_target="192.0.2.10",
    )

    assert compiled == {
        "target": "192.0.2.10",
        "interface": "eth0",
        "protocol": "tcp",
        "host": "192.0.2.10",
        "port": 443,
        "bpf_filter": "tcp[tcpflags] & tcp-syn != 0",
        "quiet": True,
        "include_payload": True,
    }
    assert {
        "timeout",
        "packet_count",
        "duration_seconds",
        "snaplen",
        "write_file",
    }.isdisjoint(compiled)


def test_tcpdump_timeout_policy_does_not_inject_hidden_parameters() -> None:
    timeout_plan = resolve_tool_timeout_plan(
        tool_id=TCPDUMP_TOOL_ID,
        parameters={"target": "unused"},
    )

    assert timeout_plan.normalized_parameters == {"target": "unused"}


def test_tcpdump_command_is_bounded_by_default() -> None:
    command = TcpdumpTool().build_command(TcpdumpArgs(target="unused"))

    assert command[:3] == [
        "timeout",
        f"{TCPDUMP_HARD_TIMEOUT_SECONDS}s",
        "tcpdump",
    ]
    assert _flag_value(command, "-c") == str(TCPDUMP_PACKET_LIMIT)
    assert _flag_value(command, "-s") == str(TCPDUMP_DEFAULT_SNAPLEN)
    assert _flag_value(command, "-w").startswith("artifacts/tcpdump_")
    assert _flag_value(command, "-w").endswith(".pcap")
    assert "-G" not in command
    assert "-A" not in command


def test_tcpdump_include_payload_keeps_pcap_output_without_ascii_stdout() -> None:
    command = TcpdumpTool().build_command(
        TcpdumpArgs(target="unused", include_payload=True)
    )

    assert _flag_value(command, "-w").endswith(".pcap")
    assert "-A" not in command
    assert "-X" not in command

    result = validate_tool_command(
        TcpdumpTool(),
        TcpdumpArgs(target="unused", include_payload=True),
        TCPDUMP_TOOL_ID,
    )
    assert result.valid, result.errors


def test_tcpdump_direct_packet_count_is_clamped_to_tool_limit() -> None:
    command = TcpdumpTool().build_command(
        TcpdumpArgs(target="unused", packet_count=TCPDUMP_PACKET_LIMIT + 1)
    )

    assert _flag_value(command, "-c") == str(TCPDUMP_PACKET_LIMIT)


def test_tcpdump_direct_lower_packet_count_is_respected() -> None:
    command = TcpdumpTool().build_command(TcpdumpArgs(target="unused", packet_count=5))

    assert _flag_value(command, "-c") == "5"


def test_tcpdump_duration_seconds_does_not_emit_rotation_flag() -> None:
    command = TcpdumpTool().build_command(
        TcpdumpArgs(target="unused", duration_seconds=5)
    )

    assert "-G" not in command
    assert _flag_value(command, "-c") == str(TCPDUMP_PACKET_LIMIT)


def test_tcpdump_rejects_unsafe_capture_paths() -> None:
    for write_file in ("/tmp/capture.pcap", "../capture.pcap", "capture.txt"):
        with pytest.raises(ValidationError):
            TcpdumpArgs(target="unused", write_file=write_file)


def test_tcpdump_create_artifacts_returns_pcap_without_text_artifact(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    capture_path = "artifacts/test_capture.pcap"
    args = TcpdumpArgs(target="unused", write_file=capture_path)
    command = TcpdumpTool().build_command(args)

    (tmp_path / capture_path).write_bytes(b"pcap")
    artifacts = TcpdumpTool().create_artifacts(
        "decoded tcpdump stdout that should not be persisted as text",
        args,
        timestamp=123,
    )

    assert _flag_value(command, "-w") == capture_path
    assert artifacts == [capture_path]
    assert not (tmp_path / "artifacts" / "tcpdump_123.txt").exists()


def test_tcpdump_timeout_exit_code_is_successful_bounded_completion() -> None:
    args = TcpdumpArgs(target="unused")
    tool = TcpdumpTool()

    assert tool.is_success_exit_code(0, args)
    assert tool.is_success_exit_code(TCPDUMP_TIMEOUT_EXIT_CODE, args)
    assert not tool.is_success_exit_code(1, args)


def test_tcpdump_timeout_wrapped_command_passes_command_validation() -> None:
    args = TcpdumpArgs(target="unused")
    result = validate_tool_command(
        TcpdumpTool(),
        args,
        TCPDUMP_TOOL_ID,
    )

    assert result.valid, result.errors


def test_tcpdump_metadata_allows_parallel_capture_with_http_trigger() -> None:
    metadata = get_enhanced_tool_metadata(TCPDUMP_TOOL_ID)

    assert metadata is not None
    assert metadata.parallel_compatible is True
    assert metadata.max_concurrent_per_target == 1

    batch = ToolBatch(
        tool_batch_id="tb_tcpdump_trigger",
        tool_calls=(
            ToolCall(
                tool_call_id="tc_capture",
                tool_id=TCPDUMP_TOOL_ID,
                parameters={"target": "unused"},
            ),
            ToolCall(
                tool_call_id="tc_trigger",
                tool_id="information_gathering.web_enumeration.http_request",
                parameters={
                    "target": "example.test",
                    "url": "http://example.test",
                },
            ),
        ),
        requested_execution_strategy=ExecutionStrategy.PARALLEL,
    )
    verdict = BatchCompatibilityChecker().check(batch)

    assert verdict.outcome is CompatibilityOutcome.PARALLEL_OK
    assert verdict.effective_strategy is ExecutionStrategy.PARALLEL
    assert verdict.reason is None
