"""Command contract tests for TShark intent profiles against a PCAP artifact."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any

import pytest

from agent.tools.sniffing_spoofing.network_sniffers.tshark import (
    TSharkArgs,
    TSharkTool,
)


DOWNLOAD_0_PCAP = "artifacts/download_0.pcap"
CONTAINER_DOWNLOAD_0_PCAP = "/workspace/artifacts/download_0.pcap"


def _flag_value(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def _all_flag_values(command: list[str], flag: str) -> list[str]:
    return [command[index + 1] for index, item in enumerate(command) if item == flag]


def _workspace_with_download_0(tmp_path: Path) -> Path:
    """Return a workspace containing artifacts/download_0.pcap."""

    candidate = Path(".drowai-runner-cloud/tasks/task-119").resolve()
    if (candidate / DOWNLOAD_0_PCAP).is_file():
        return candidate
    workspace = tmp_path
    (workspace / "artifacts").mkdir(parents=True, exist_ok=True)
    (workspace / DOWNLOAD_0_PCAP).write_bytes(b"pcap")
    return workspace


INTENT_CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "survey",
        "params": {
            "analysis_mode": "survey",
            "input_file": DOWNLOAD_0_PCAP,
            "max_rows": 100,
        },
        "expected_display_filter_fragments": (),
        "expected_output_fields": (
            "frame.number",
            "frame.time_epoch",
            "frame.protocols",
            "ip.src",
            "ip.dst",
            "ipv6.src",
            "ipv6.dst",
            "tcp.srcport",
            "tcp.dstport",
            "tcp.stream",
            "udp.srcport",
            "udp.dstport",
            "frame.len",
            "dns.qry.name",
            "dns.flags.rcode",
            "http.host",
            "http.request.method",
            "http.request.uri",
            "http.response.code",
            "tls.handshake.extensions_server_name",
            "tls.alert_message.desc",
            "ftp.request.command",
            "smtp.req.command",
            "pop.request.command",
            "imap.request.command",
            "tcp.analysis.retransmission",
            "tcp.analysis.fast_retransmission",
            "tcp.analysis.lost_segment",
            "tcp.analysis.duplicate_ack",
            "icmp.type",
            "icmp.code",
        ),
    },
    {
        "id": "anomaly_detection",
        "params": {
            "analysis_mode": "anomaly_detection",
            "input_file": DOWNLOAD_0_PCAP,
            "max_rows": 100,
        },
        "expected_display_filter_fragments": (
            "tcp.analysis.retransmission",
            "dns.flags.rcode != 0",
            "http.response.code >= 400",
            "tls.alert_message",
        ),
        "expected_output_fields": (
            "frame.number",
            "frame.time_epoch",
            "frame.protocols",
            "ip.src",
            "ip.dst",
            "ipv6.src",
            "ipv6.dst",
            "tcp.srcport",
            "tcp.dstport",
            "tcp.stream",
            "udp.srcport",
            "udp.dstport",
            "tcp.analysis.retransmission",
            "tcp.analysis.fast_retransmission",
            "tcp.analysis.lost_segment",
            "tcp.analysis.duplicate_ack",
            "icmp.type",
            "icmp.code",
            "dns.flags.rcode",
            "http.response.code",
            "tls.alert_message.desc",
        ),
    },
    {
        "id": "investigate_protocol_http",
        "params": {
            "analysis_mode": "investigate_protocol",
            "input_file": DOWNLOAD_0_PCAP,
            "protocol": "http",
            "max_rows": 100,
        },
        "expected_display_filter_fragments": ("http",),
        "expected_output_fields": (
            "frame.number",
            "frame.time_epoch",
            "frame.protocols",
            "ip.src",
            "ip.dst",
            "ipv6.src",
            "ipv6.dst",
            "tcp.srcport",
            "tcp.dstport",
            "tcp.stream",
            "udp.srcport",
            "udp.dstport",
            "http.host",
            "http.request.method",
            "http.request.uri",
            "http.response.code",
            "http.user_agent",
            "http.content_type",
            "http.authorization",
            "http.cookie",
            "http.set_cookie",
        ),
    },
    {
        "id": "extract_evidence_stream",
        "params": {
            "analysis_mode": "extract_evidence",
            "input_file": DOWNLOAD_0_PCAP,
            "stream_id": 3,
            "fields": [
                "frame.number",
                "frame.protocols",
                "ip.src",
                "ip.dst",
                "tcp.stream",
                "http.authorization",
            ],
            "max_rows": 50,
        },
        "expected_display_filter_fragments": ("tcp.stream == 3",),
        "expected_output_fields": (
            "frame.number",
            "frame.protocols",
            "ip.src",
            "ip.dst",
            "tcp.stream",
            "http.authorization",
        ),
    },
    {
        "id": "find_security_relevant_artifacts",
        "params": {
            "analysis_mode": "find_security_relevant_artifacts",
            "input_file": DOWNLOAD_0_PCAP,
            "terms": ["password", "token"],
            "max_rows": 100,
        },
        "expected_display_filter_fragments": (
            "http.authorization",
            "http.cookie",
            "ftp.request.command",
            'frame contains "password"',
            'frame contains "token"',
        ),
        "expected_output_fields": (
            "frame.number",
            "frame.time_epoch",
            "frame.protocols",
            "ip.src",
            "ip.dst",
            "ipv6.src",
            "ipv6.dst",
            "tcp.srcport",
            "tcp.dstport",
            "tcp.stream",
            "udp.srcport",
            "udp.dstport",
            "http.host",
            "http.request.method",
            "http.request.uri",
            "http.authorization",
            "http.cookie",
            "http.set_cookie",
            "ftp.request.command",
            "ftp.request.arg",
            "smtp.req.command",
            "smtp.req.parameter",
            "pop.request.command",
            "pop.request.parameter",
            "imap.request.command",
            "imap.request",
        ),
    },
)


@pytest.mark.parametrize(
    "case",
    INTENT_CASES,
    ids=[case["id"] for case in INTENT_CASES],
)
def test_tshark_intent_profile_command_for_download_0_pcap(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """List the exact in-container command and output columns for each intent."""

    monkeypatch.setenv("WORKSPACE", str(_workspace_with_download_0(tmp_path)))
    params = TSharkTool.compile_planner_parameters(case["params"])
    command = TSharkTool().build_command(TSharkArgs(**params))

    assert command[0] == "tshark"
    assert _flag_value(command, "-r") == CONTAINER_DOWNLOAD_0_PCAP
    assert _flag_value(command, "-T") == "fields"
    assert _flag_value(command, "-c") == str(case["params"]["max_rows"])
    assert _all_flag_values(command, "-e") == list(case["expected_output_fields"])

    if case["expected_display_filter_fragments"]:
        display_filter = _flag_value(command, "-Y")
        for fragment in case["expected_display_filter_fragments"]:
            assert fragment in display_filter
    else:
        assert "-Y" not in command

    # The assertion message intentionally exposes the command contract when a
    # profile changes, making the Kali-visible invocation easy to inspect.
    assert shlex.join(command)


def test_tshark_intent_profile_command_matrix_lists_outputs() -> None:
    """Keep the per-intent output-column matrix explicit and non-empty."""

    assert {case["id"] for case in INTENT_CASES} == {
        "survey",
        "anomaly_detection",
        "investigate_protocol_http",
        "extract_evidence_stream",
        "find_security_relevant_artifacts",
    }
    for case in INTENT_CASES:
        assert case["expected_output_fields"], case["id"]
