"""Tests for dual process/model output channels in command-transport enrichment."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent.tool_runtime.result_enrichment import build_command_transport_tool_result
from agent.tools.information_gathering.network_discovery.fping import FpingArgs, FpingTool
from agent.tools.sniffing_spoofing.network_sniffers.tshark import (
    TSharkAnalysisMode,
    TSharkArgs,
    TSharkTool,
)


def test_build_command_transport_exposes_process_and_model_stdio(tmp_path) -> None:
    """Rendered stdout must not replace raw process stdout on the result object."""
    raw_stdout = "172.17.0.1\n"
    raw_stderr = "172.17.0.247 : xmt/rcv/%loss = 1/0/100%\n"
    shell_result = SimpleNamespace(
        stdout=raw_stdout,
        stderr=raw_stderr,
        exit_code=1,
    )

    result = build_command_transport_tool_result(
        tool=FpingTool(),
        args=FpingArgs(target="172.17.0.0/24"),
        shell_result=shell_result,
        command="fping -a -s -g 172.17.0.0/24",
        host_workspace_path=str(tmp_path),
        include_stderr_in_artifacts=True,
        artifact_stamp=111,
    )

    assert result.process_stdout == raw_stdout
    assert result.process_stderr == raw_stderr
    assert "Alive hosts: 1" in result.stdout
    assert "xmt/rcv/%loss" not in result.stdout
    assert result.stderr == ""


def test_build_command_transport_skips_renderer_for_hard_cli_failure(tmp_path) -> None:
    """Hard CLI failures must not be reinterpreted as tool-domain output."""
    raw_stdout = "bash: line 1: fping: command not found\n"
    shell_result = SimpleNamespace(
        stdout=raw_stdout,
        stderr="",
        exit_code=127,
    )

    result = build_command_transport_tool_result(
        tool=FpingTool(),
        args=FpingArgs(target="172.17.0.0", range_end="172.17.0.255"),
        shell_result=shell_result,
        command="fping -a -s -r 1 -p 1000 -g 172.17.0.0 172.17.0.255",
        host_workspace_path=str(tmp_path),
        include_stderr_in_artifacts=True,
        artifact_stamp=112,
    )

    assert result.success is False
    assert result.exit_code == 127
    assert result.process_stdout == raw_stdout
    assert result.process_stderr == ""
    assert result.stdout == ""
    assert result.stderr == raw_stdout
    assert "Alive hosts:" not in result.stdout
    assert "alive_count" not in getattr(result, "metadata", {})


def test_build_command_transport_tshark_compacts_model_stdout_and_keeps_process_stdout(tmp_path) -> None:
    """TShark compact stdout must not replace raw process stdout on the result object."""
    raw_stdout = json.dumps(
        [
            {
                "_source": {
                    "layers": {
                        "frame": {
                            "frame.number": "1",
                            "frame.protocols": "eth:ip:tcp:http",
                        },
                        "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.20"},
                        "tcp": {
                            "tcp.srcport": "49152",
                            "tcp.dstport": "80",
                            "tcp.stream": "7",
                        },
                        "http": {"http.authorization": "Bearer raw-runtime-token"},
                    }
                }
            }
        ]
    )
    shell_result = SimpleNamespace(stdout=raw_stdout, stderr="", exit_code=0)

    result = build_command_transport_tool_result(
        tool=TSharkTool(),
        args=TSharkArgs(
            target="unused",
            analysis_mode=TSharkAnalysisMode.FIND_SECURITY_RELEVANT_ARTIFACTS,
        ),
        shell_result=shell_result,
        command="tshark -T json",
        host_workspace_path=str(tmp_path),
    )

    compact_stdout = json.loads(result.stdout)
    assert result.process_stdout == raw_stdout
    assert compact_stdout["schema_version"] == "pcap.compact.v1"
    assert "_source" not in result.stdout
    assert "raw-runtime-token" in result.stdout
    assert result.metadata["pcap_compact"]["schema_version"] == "pcap.compact.v1"
    assert result.metadata["compact_key_findings"]
    assert result.stderr == ""
