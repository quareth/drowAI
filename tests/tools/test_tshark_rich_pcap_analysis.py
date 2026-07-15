"""Regression tests for TShark rich PCAP planner guardrails."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.semantic.enrichment import validate_semantic_evidence_entries
from agent.semantic.evidence_vocabulary import SemanticEvidenceType
from agent.tools.canonical_capture import CanonicalCaptureFormat, CaptureFamily
from agent.tools.sniffing_spoofing.network_sniffers import tshark as tshark_module
from agent.tools.sniffing_spoofing.network_sniffers import tshark_semantics
from agent.tools.sniffing_spoofing.network_sniffers.tshark import (
    TSHARK_DEFAULT_SNAPLEN,
    TSHARK_HARD_TIMEOUT_SECONDS,
    TSHARK_LIVE_PACKET_LIMIT,
    TSHARK_SAFE_TARGET_PLACEHOLDER,
    TSHARK_TIMEOUT_EXIT_CODE,
    TSharkAnalysisMode,
    TSharkArgs,
    TSharkPlannerArgs,
    TSharkTool,
)
from tests.tools.validation.command_validator import CommandValidator


def _flag_value(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def _tshark_json(*layers: dict) -> str:
    return json.dumps([{"_source": {"layers": layer}} for layer in layers])


def test_tshark_parser_recovers_complete_rows_from_truncated_json_array() -> None:
    """Runtime-truncated TShark JSON arrays should preserve decoded packet rows."""
    stdout = _tshark_json(
        {
            "frame": {
                "frame.number": "36",
                "frame.time_epoch": "1718360001.0",
                "frame.protocols": "sll:ethertype:ip:tcp:ftp",
                "frame.len": "74",
            },
            "ip": {"ip.src": "192.168.196.1", "ip.dst": "192.168.196.16"},
            "tcp": {"tcp.srcport": "42310", "tcp.dstport": "21", "tcp.stream": "3"},
            "ftp": {"ftp.request.command": "USER", "ftp.request.arg": "nathan"},
        },
        {
            "frame": {
                "frame.number": "40",
                "frame.time_epoch": "1718360002.0",
                "frame.protocols": "sll:ethertype:ip:tcp:ftp",
                "frame.len": "86",
            },
            "ip": {"ip.src": "192.168.196.1", "ip.dst": "192.168.196.16"},
            "tcp": {"tcp.srcport": "42310", "tcp.dstport": "21", "tcp.stream": "3"},
            "ftp": {"ftp.request.command": "PASS", "ftp.request.arg": "Buck3tH4TF0RM3!"},
        },
        {
            "frame": {
                "frame.number": "42",
                "frame.time_epoch": "1718360003.0",
                "frame.protocols": "sll:ethertype:ip:tcp:ftp",
                "frame.len": "70",
            },
            "ip": {"ip.src": "192.168.196.16", "ip.dst": "192.168.196.1"},
            "tcp": {"tcp.srcport": "21", "tcp.dstport": "42310", "tcp.stream": "3"},
            "ftp": {"ftp.response.code": "230", "ftp.response.arg": "Login successful."},
        },
    )[:-1]

    metadata = tshark_semantics.parse_tshark_output(
        stdout,
        "",
        analysis_mode="pcap_summary",
        input_file="artifacts/download_0.pcap",
        max_rows=100,
        sensitive_proof_mode="proof_excerpt",
    )

    assert metadata["pcap"]["packet_count"] == 3
    assert metadata["limits"]["input_rows_truncated"] is True
    assert metadata["limits"]["truncated"] is True
    assert [event["proof_excerpt"] for event in metadata["credential_events"]] == [
        "nathan",
        "Buck3tH4TF0RM3!",
    ]
    assert metadata["auth_sequences"][0]["success_messages"] == ["230"]


def test_tshark_capture_contract_is_structured_json() -> None:
    contract = TSharkTool().capture_contract()

    assert contract is not None
    assert contract.family is CaptureFamily.STRUCTURED_NATIVE
    assert contract.canonical_format is CanonicalCaptureFormat.JSON
    assert contract.is_structured


def test_tshark_input_file_normalizes_executor_workspace_absolute_path() -> None:
    planner_args = TSharkPlannerArgs(
        analysis_mode=TSharkAnalysisMode.SURVEY,
        input_file="/workspace/artifacts/download_0.pcap",
    )
    runtime_args = TSharkArgs(
        target="unused",
        input_file="/workspace/artifacts/download_0.pcap",
    )

    assert planner_args.input_file == "artifacts/download_0.pcap"
    assert runtime_args.input_file == "artifacts/download_0.pcap"

    compiled = TSharkTool.compile_planner_parameters(planner_args)

    assert compiled["input_file"] == "artifacts/download_0.pcap"


def test_tshark_input_file_rejects_non_workspace_absolute_path() -> None:
    with pytest.raises(ValidationError, match="workspace-relative"):
        TSharkPlannerArgs(
            analysis_mode=TSharkAnalysisMode.SURVEY,
            input_file="/tmp/download_0.pcap",
        )


def test_tshark_enhanced_metadata_describes_safe_pcap_analysis_behavior() -> None:
    from agent.tools.enhanced_metadata_registry import get_enhanced_tool_metadata
    from agent.tools.enhanced_tool_metadata import (
        build_rich_tool_description,
        build_tool_catalog_entries,
    )

    tool_id = "sniffing_spoofing.network_sniffers.tshark"
    metadata = get_enhanced_tool_metadata(tool_id)

    assert metadata is not None
    assert metadata.parallel_compatible is False

    descriptions = " ".join(cap.description for cap in metadata.capabilities).lower()
    assert "passive pcap" in descriptions
    assert "offline artifacts" in descriptions
    assert "finite live capture" in descriptions
    assert "credential/key exposure" in descriptions
    assert "bounded proof" in descriptions
    assert "tcpdump" in descriptions
    assert "artifact.read" in descriptions
    assert "artifact.search" in descriptions
    assert "raw secret extraction" not in descriptions
    assert "extract raw secret" not in descriptions

    indicators = {
        indicator
        for capability in metadata.capabilities
        for indicator in capability.output_indicators
    }
    assert {
        "structured_metadata",
        "semantic_observations",
        "semantic_evidence",
        "packet_proof",
        "evidence_refs",
    }.issubset(indicators)

    catalog_description = build_tool_catalog_entries([tool_id])[0]["description"]
    assert len(catalog_description) <= 200
    assert "semantic_observations" in catalog_description
    assert "semantic_evidence" in catalog_description
    assert "bounded proof" in catalog_description

    rich_description = build_rich_tool_description(tool_id)
    assert "artifact.read" in rich_description
    assert "artifact.search" in rich_description


def test_tshark_rich_parsing_delegates_to_semantics_module() -> None:
    assert tshark_module.parse_tshark_output is tshark_semantics.parse_tshark_output

    module_source = Path(tshark_semantics.__file__).read_text(encoding="utf-8")
    assert module_source.startswith('"""Rich TShark parsing and semantic helper boundary.')
    assert "\nimport backend" not in module_source
    assert "\nfrom backend" not in module_source
    assert "\ndef _" not in module_source
    assert "from .tshark_parsing.parser import parse_tshark_output" in module_source


def test_tshark_semantics_returns_stable_empty_metadata_contract() -> None:
    metadata = tshark_semantics.parse_tshark_output(
        "",
        "",
        analysis_mode="http",
        input_file="captures/example.pcap",
        max_rows=5,
    )

    assert list(metadata) == [
        "schema_version",
        "analysis_mode",
        "pcap",
        "protocols",
        "hosts",
        "conversations",
        "services",
        "interesting_streams",
        "recommended_next_queries",
        "dns",
        "http",
        "tls",
        "ftp",
        "auth_indicators",
        "secret_exposure",
        "credential_events",
        "auth_sequences",
        "field_extract",
        "limits",
        "warnings",
        "errors",
    ]
    assert metadata["schema_version"] == "tshark.v1"
    assert metadata["analysis_mode"] == "http"
    assert metadata["pcap"] == {
        "input_file": "captures/example.pcap",
        "artifact_sha256": None,
        "packet_count": 0,
        "duration_seconds": None,
    }
    assert metadata["protocols"] == []
    assert metadata["hosts"] == []
    assert metadata["conversations"] == []
    assert metadata["services"] == []
    assert metadata["interesting_streams"] == []
    assert metadata["recommended_next_queries"] == []
    assert metadata["dns"] == []
    assert metadata["http"] == []
    assert metadata["tls"] == []
    assert metadata["ftp"] == []
    assert metadata["auth_indicators"] == []
    assert metadata["secret_exposure"] == []
    assert metadata["credential_events"] == []
    assert metadata["auth_sequences"] == []
    assert metadata["field_extract"] == []
    assert metadata["warnings"] == []
    assert metadata["errors"] == []
    assert metadata["limits"] == {"max_rows": 5, "truncated": False, "lists": {}}


def test_tshark_survey_field_output_emits_routing_hints() -> None:
    fields = [
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
    ]
    stdout = "\n".join(
        [
            "\t".join(
                [
                    "1",
                    "100.0",
                    "sll:ethertype:ip:tcp:http",
                    "192.0.2.10",
                    "192.0.2.20",
                    "",
                    "",
                    "49152",
                    "80",
                    "0",
                    "",
                    "",
                    "96",
                    "",
                    "",
                    "192.0.2.20",
                    "GET",
                    "/",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            ),
            "\t".join(
                [
                    "2",
                    "101.5",
                    "sll:ethertype:ip:tcp:http:data-text-lines",
                    "192.0.2.20",
                    "192.0.2.10",
                    "",
                    "",
                    "80",
                    "49152",
                    "0",
                    "",
                    "",
                    "128",
                    "",
                    "",
                    "",
                    "",
                    "/favicon.ico",
                    "404",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            ),
            "\t".join(
                [
                    "3",
                    "103.0",
                    "sll:ethertype:ip:tcp:ftp",
                    "192.0.2.10",
                    "192.0.2.21",
                    "",
                    "",
                    "49153",
                    "21",
                    "1",
                    "",
                    "",
                    "74",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "PASS",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                ]
            ),
        ]
    )

    metadata = tshark_semantics.parse_tshark_output(
        stdout,
        "",
        analysis_mode="survey",
        input_file="artifacts/download_0.pcap",
        max_rows=100,
        fields=fields,
    )

    assert metadata["pcap"]["packet_count"] == 3
    assert metadata["pcap"]["duration_seconds"] == 3.0
    assert metadata["pcap"]["shape"] == {
        "packet_count": 3,
        "time_start": 100.0,
        "time_end": 103.0,
        "duration_seconds": 3.0,
    }
    assert {service["protocol_hint"] for service in metadata["services"]} >= {"http", "ftp"}
    assert any(
        stream["recommended_intent"] == "find_security_relevant_artifacts"
        and stream["protocol_hint"] == "ftp"
        for stream in metadata["interesting_streams"]
    )
    assert any(
        query["intent"] == "find_security_relevant_artifacts"
        and query["params"]["protocol"] == "ftp"
        for query in metadata["recommended_next_queries"]
    )
    assert any(
        query["intent"] == "anomaly_detection"
        and query["params"]["protocol"] == "http"
        for query in metadata["recommended_next_queries"]
    )


def test_tshark_mode_parsers_extract_protocol_rows_and_conversations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(tshark_semantics.SECRET_FINGERPRINT_KEY_ENV, "unit-test-hmac-key")
    stdout = _tshark_json(
        {
            "frame": {
                "frame.number": "1",
                "frame.time_epoch": "100.0",
                "frame.time": "Jun 14, 2026 10:00:00.000000000 UTC",
                "frame.protocols": "eth:ip:udp:dns",
                "frame.len": "86",
            },
            "ip": {"ip.src": "192.0.2.10", "ip.dst": "198.51.100.53"},
            "udp": {"udp.srcport": "53123", "udp.dstport": "53"},
            "dns": {
                "dns.qry.name": "www.example.test",
                "dns.qry.type": "1",
                "dns.a": ["198.51.100.10", "198.51.100.10"],
                "dns.flags.rcode": "0",
            },
        },
        {
            "frame": {
                "frame.number": "2",
                "frame.time_epoch": "101.5",
                "frame.time": "Jun 14, 2026 10:00:01.500000000 UTC",
                "frame.protocols": "eth:ip:tcp:http",
                "frame.len": "512",
            },
            "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.20"},
            "tcp": {"tcp.srcport": "49152", "tcp.dstport": "80", "tcp.stream": "7"},
            "http": {
                "http.host": "app.example.test",
                "http.request.method": "GET",
                "http.request.uri": "http://app.example.test/login?next=/admin",
                "http.response.code": "200",
                "http.user_agent": "SyntheticAgent/1.0",
                "http.server": "nginx",
                "http.authorization": "Bearer synthetic-http-token",
                "http.cookie": "session=synthetic-cookie-secret",
            },
        },
        {
            "frame": {
                "frame.number": "3",
                "frame.time_epoch": "103.0",
                "frame.time": "Jun 14, 2026 10:00:03.000000000 UTC",
                "frame.protocols": "eth:ip:tcp:tls",
                "frame.len": "256",
            },
            "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.443"},
            "tcp": {"tcp.srcport": "49153", "tcp.dstport": "443", "tcp.stream": "8"},
            "tls": {
                "tls.handshake.extensions_server_name": "secure.example.test",
                "tls.handshake.extensions_alpn_str": "h2",
                "tls.handshake.version": "0x0303",
            },
            "x509sat": {
                "x509sat.uTF8String": "CN=secure.example.test",
                "x509sat.printableString": "Issuer Test CA",
                "x509sat.subject": "CN=secure.example.test",
                "x509sat.issuer": "CN=Issuer Test CA",
            },
        },
        {
            "frame": {
                "frame.number": "4",
                "frame.time_epoch": "104.0",
                "frame.protocols": "eth:ip:tcp:ftp",
                "frame.len": "128",
            },
            "ip": {"ip.src": "192.0.2.20", "ip.dst": "203.0.113.21"},
            "tcp": {"tcp.srcport": "49154", "tcp.dstport": "21", "tcp.stream": "9"},
            "ftp": {
                "ftp.request.command": "PASS",
                "ftp.request.command_parameter": "synthetic-ftp-password",
            },
        },
    )

    summary = tshark_semantics.parse_tshark_output(stdout, "", analysis_mode="pcap_summary")
    conversations = tshark_semantics.parse_tshark_output(
        stdout,
        "",
        analysis_mode="conversations",
    )
    dns = tshark_semantics.parse_tshark_output(stdout, "", analysis_mode="dns")
    http = tshark_semantics.parse_tshark_output(stdout, "", analysis_mode="http")
    tls = tshark_semantics.parse_tshark_output(stdout, "", analysis_mode="tls")
    auth = tshark_semantics.parse_tshark_output(stdout, "", analysis_mode="auth_indicators")
    exposure = tshark_semantics.parse_tshark_output(
        stdout,
        "",
        analysis_mode="secret_exposure",
        artifact_sha256="synthetic-pcap-sha256",
    )

    assert summary["pcap"]["packet_count"] == 4
    assert summary["pcap"]["duration_seconds"] == 4.0
    assert {"dns", "http", "tls", "ftp"}.issubset(summary["protocols"])
    assert any(item["flow_key"] == "tcp:192.0.2.10:49152->203.0.113.20:80" for item in conversations["conversations"])

    assert dns["dns"] == [
        {
            "frame": "1",
            "time": "Jun 14, 2026 10:00:00.000000000 UTC",
            "query": "www.example.test",
            "qtype": "1",
            "answers": ["198.51.100.10"],
            "rcode": "0",
            "src": "192.0.2.10",
            "dst": "198.51.100.53",
        }
    ]
    assert http["http"][0]["path"] == "/login?next=/admin"
    assert http["http"][0]["headers"]["http.authorization"][0] == "Bearer synthetic-http-token"
    assert tls["tls"][0]["sni"] == "secure.example.test"
    assert tls["tls"][0]["subject"] == "CN=secure.example.test"
    assert "0x0303" in tls["tls"][0]["versions"]
    assert {item["mechanism"] for item in auth["auth_indicators"]} >= {
        "authorization",
        "cookie",
        "protocol_auth",
    }
    assert exposure["secret_exposure"]
    assert all(item["proof_mode"] == "proof_excerpt" for item in exposure["secret_exposure"])
    assert all("fingerprint" not in item for item in exposure["secret_exposure"])
    ftp_exposure = next(
        item
        for item in exposure["secret_exposure"]
        if item["field"] == "ftp.request.command_parameter"
    )
    assert ftp_exposure["protocol"] == "ftp"
    assert ftp_exposure["flow_key"] == "tcp:192.0.2.20:49154->203.0.113.21:21"
    assert ftp_exposure["pcap_artifact_sha256"] == "synthetic-pcap-sha256"
    assert ftp_exposure["extraction_filter"] == "ftp.request.command == PASS"
    assert ftp_exposure["proof_excerpt"] == "synthetic-ftp-password"
    http_auth_exposure = next(
        item for item in exposure["secret_exposure"] if item["field"] == "http.authorization"
    )
    assert http_auth_exposure["protocol"] == "http"
    assert http_auth_exposure["extraction_filter"] == "http.authorization"

    serialized = str([http, auth, exposure])
    assert "synthetic-http-token" in serialized
    assert "synthetic-cookie-secret" in serialized
    assert "synthetic-ftp-password" in serialized


def test_tshark_extracts_nested_ftp_user_pass_arg_values() -> None:
    stdout = _tshark_json(
        {
            "frame": {
                "frame.number": "36",
                "frame.time": "2021-05-14T13:12:54.084642000Z",
                "frame.protocols": "sll:ethertype:ip:tcp:ftp",
            },
            "ip": {"ip.src": "192.168.196.1", "ip.dst": "192.168.196.16"},
            "tcp": {"tcp.srcport": "54411", "tcp.dstport": "21", "tcp.stream": "3"},
            "ftp": {
                "USER nathan\r\n": {
                    "ftp.request.command": "USER",
                    "ftp.request.arg": "nathan",
                }
            },
        },
        {
            "frame": {
                "frame.number": "40",
                "frame.time": "2021-05-14T13:12:55.383140000Z",
                "frame.protocols": "sll:ethertype:ip:tcp:ftp",
            },
            "ip": {"ip.src": "192.168.196.1", "ip.dst": "192.168.196.16"},
            "tcp": {"tcp.srcport": "54411", "tcp.dstport": "21", "tcp.stream": "3"},
            "ftp": {
                "PASS Buck3tH4TF0RM3!\r\n": {
                    "ftp.request.command": "PASS",
                    "ftp.request.arg": "Buck3tH4TF0RM3!",
                }
            },
        },
        {
            "frame": {
                "frame.number": "42",
                "frame.time": "2021-05-14T13:12:55.390529000Z",
                "frame.protocols": "sll:ethertype:ip:tcp:ftp",
            },
            "ip": {"ip.src": "192.168.196.16", "ip.dst": "192.168.196.1"},
            "tcp": {"tcp.srcport": "21", "tcp.dstport": "54411", "tcp.stream": "3"},
            "ftp": {
                "230 Login successful.\r\n": {
                    "ftp.response.code": "230",
                    "ftp.response.arg": "Login successful.",
                }
            },
        },
    )

    summary = tshark_semantics.parse_tshark_output(stdout, "", analysis_mode="pcap_summary")
    exposure = tshark_semantics.parse_tshark_output(stdout, "", analysis_mode="secret_exposure")

    assert any(
        event["role"] == "username" and event["proof_excerpt"] == "nathan"
        for event in summary["credential_events"]
    )
    assert any(
        event["role"] == "password" and event["proof_excerpt"] == "Buck3tH4TF0RM3!"
        for event in summary["credential_events"]
    )
    assert summary["auth_sequences"][0]["success_count"] == 1
    password_exposure = next(
        item for item in exposure["secret_exposure"] if item["field"] == "ftp.request.arg"
    )
    assert password_exposure["proof_excerpt"] == "Buck3tH4TF0RM3!"
    assert password_exposure["extraction_filter"] == "ftp.request.command == PASS"


def test_tshark_field_extract_parser_maps_allowlisted_fields() -> None:
    metadata = tshark_semantics.parse_tshark_output(
        "1\t192.0.2.10\texample.test\n",
        "",
        analysis_mode="field_extract",
        fields=["frame.number", "ip.src", "dns.qry.name"],
    )

    assert metadata["pcap"]["packet_count"] == 1
    assert metadata["field_extract"] == [
        {
            "row": 1,
            "fields": {
                "frame.number": "1",
                "ip.src": "192.0.2.10",
                "dns.qry.name": "example.test",
            },
        }
    ]


def test_tshark_field_extract_ignores_root_warning_rows() -> None:
    metadata = tshark_semantics.parse_tshark_output(
        "\n".join(
            [
                'Running as user "root" and group "root". This could be dangerous.',
                "36\t192.168.196.1\t192.168.196.16",
            ]
        ),
        "",
        analysis_mode="extract_evidence",
        fields=["frame.number", "ip.src", "ip.dst"],
    )

    assert metadata["pcap"]["packet_count"] == 1
    assert metadata["field_extract"] == [
        {
            "row": 1,
            "fields": {
                "frame.number": "36",
                "ip.src": "192.168.196.1",
                "ip.dst": "192.168.196.16",
            },
        }
    ]
    assert "Running as user" not in str(metadata["field_extract"])
    assert any("discarded non-data field row 1" in warning for warning in metadata["warnings"])


def test_tshark_ftp_field_output_promotes_protocol_and_security_signals() -> None:
    fields = [
        "frame.number",
        "frame.time",
        "ip.src",
        "ip.dst",
        "tcp.stream",
        "tcp.srcport",
        "tcp.dstport",
        "ftp.request.command",
        "ftp.request.arg",
        "ftp.response.code",
        "ftp.response.arg",
    ]
    stdout = "\n".join(
        [
            'Running as user "root" and group "root". This could be dangerous.',
            "34\t2021-05-14T13:12:52.585037000+0000\t192.168.196.16\t192.168.196.1\t3\t21\t54411\t\t\t220\t(vsFTPd 3.0.3)",
            "36\t2021-05-14T13:12:54.084642000+0000\t192.168.196.1\t192.168.196.16\t3\t54411\t21\tUSER\tnathan\t\t",
            "40\t2021-05-14T13:12:55.383140000+0000\t192.168.196.1\t192.168.196.16\t3\t54411\t21\tPASS\tBuck3tH4TF0RM3!\t\t",
            "42\t2021-05-14T13:12:55.390529000+0000\t192.168.196.16\t192.168.196.1\t3\t21\t54411\t\t\t230\tLogin successful.",
        ]
    )

    metadata = tshark_semantics.parse_tshark_output(
        stdout,
        "",
        analysis_mode="extract_evidence",
        input_file="artifacts/download_0.pcap",
        max_rows=100,
        fields=fields,
    )

    assert metadata["pcap"]["packet_count"] == 4
    assert metadata["ftp"][0]["response_code"] == "220"
    assert metadata["ftp"][1]["request_command"] == "USER"
    assert metadata["ftp"][1]["request_arg"] == "nathan"
    assert metadata["ftp"][2]["request_command"] == "PASS"
    assert metadata["ftp"][2]["request_arg"] == "Buck3tH4TF0RM3!"
    assert any(
        event["role"] == "username" and event["proof_excerpt"] == "nathan"
        for event in metadata["credential_events"]
    )
    assert any(
        event["role"] == "password" and event["proof_excerpt"] == "Buck3tH4TF0RM3!"
        for event in metadata["credential_events"]
    )
    assert metadata["auth_sequences"][0]["success_count"] == 1
    assert metadata["secret_exposure"][0]["field"] == "ftp.request.arg"
    assert "No security-relevant artifact rows found" not in str(metadata["warnings"])


def test_tshark_field_rows_infer_protocol_without_frame_protocols_and_keep_tcp_len() -> None:
    metadata = tshark_semantics.parse_tshark_output(
        "50\t192.168.196.16\t192.168.196.1\t4\t20\t54412\t54411\t991",
        "",
        analysis_mode="extract_evidence",
        fields=[
            "frame.number",
            "ip.src",
            "ip.dst",
            "tcp.stream",
            "tcp.len",
            "tcp.srcport",
            "tcp.dstport",
            "frame.len",
        ],
    )

    assert metadata["conversations"][0]["protocol"] == "tcp"
    assert metadata["conversations"][0]["bytes"] == 20
    assert metadata["field_extract"][0]["fields"]["tcp.len"] == "20"


def test_tshark_runtime_http_and_auth_indicator_modes_keep_raw_values() -> None:
    raw_secret = "synthetic-runtime-token"
    stdout = _tshark_json(
        {
            "frame": {
                "frame.number": "1",
                "frame.protocols": "eth:ip:tcp:http",
            },
            "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.20"},
            "tcp": {"tcp.srcport": "49152", "tcp.dstport": "80", "tcp.stream": "7"},
            "http": {"http.authorization": f"Bearer {raw_secret}"},
        }
    )

    http = tshark_semantics.parse_tshark_output(stdout, "", analysis_mode="http")
    auth = tshark_semantics.parse_tshark_output(
        stdout,
        "",
        analysis_mode="auth_indicators",
    )

    removed_flag = "redaction" + "_applied"
    assert removed_flag not in http
    assert removed_flag not in auth
    assert raw_secret in str([http, auth])


def test_tshark_field_extract_parser_drops_non_allowlisted_fields() -> None:
    metadata = tshark_semantics.parse_tshark_output(
        "1\t68656c6c6f\t192.0.2.10\n",
        "",
        analysis_mode="field_extract",
        fields=["frame.number", "tcp.payload", "ip.src"],
    )

    assert metadata["field_extract"] == [
        {
            "row": 1,
            "fields": {
                "frame.number": "1",
                "ip.src": "192.0.2.10",
            },
        }
    ]
    assert "tcp.payload" not in str(metadata["field_extract"])
    assert "68656c6c6f" not in str(metadata)
    assert any("not allowlisted: tcp.payload" in warning for warning in metadata["warnings"])


def test_tshark_field_extract_allows_explicit_non_dotted_and_tcp_len_fields() -> None:
    metadata = tshark_semantics.parse_tshark_output(
        "1\tGET / HTTP/1.1\t17\n",
        "",
        analysis_mode="extract_evidence",
        fields=["frame.number", "data-text-lines", "tcp.len"],
    )

    assert metadata["field_extract"][0]["fields"] == {
        "frame.number": "1",
        "data-text-lines": "GET / HTTP/1.1",
        "tcp.len": "17",
    }
    args = TSharkArgs(
        target="unused",
        analysis_mode="extract_evidence",
        display_filter="ftp-data",
        fields=["frame.number", "tcp.len", "data-text-lines"],
    )
    assert args.fields == ["frame.number", "tcp.len", "data-text-lines"]


def test_tshark_missing_mode_fields_return_warnings_not_exceptions() -> None:
    stdout = _tshark_json(
        {
            "frame": {"frame.number": "1", "frame.protocols": "eth:ip:tcp"},
            "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.10"},
            "tcp": {"tcp.srcport": "49152", "tcp.dstport": "443"},
        }
    )

    metadata = tshark_semantics.parse_tshark_output(stdout, "", analysis_mode="dns")
    unknown = tshark_semantics.parse_tshark_output(stdout, "", analysis_mode="unknown_mode")

    assert metadata["dns"] == []
    assert metadata["warnings"] == ["No DNS records found in TShark JSON output."]
    assert unknown["warnings"] == ["Unknown TShark analysis mode: unknown_mode"]


def test_tshark_semantics_bounds_rows_and_records_truncation() -> None:
    stdout = "\n".join(
        [
            "1 0.000000 192.0.2.1 -> 192.0.2.2 TCP",
            "2 0.000100 192.0.2.3 -> 192.0.2.4 UDP",
            "3 0.000200 192.0.2.5 -> 192.0.2.6 HTTP",
        ]
    )

    metadata = tshark_semantics.parse_tshark_output(stdout, "", max_rows=2)

    assert metadata["pcap"]["packet_count"] == 3
    assert metadata["protocols"] == ["http", "tcp"]
    assert metadata["hosts"] == ["192.0.2.1", "192.0.2.2"]
    assert metadata["limits"]["truncated"] is True
    assert metadata["limits"]["lists"]["protocols"] == {
        "limit": 2,
        "returned": 2,
        "total": 3,
        "truncated": True,
    }
    assert metadata["limits"]["lists"]["hosts"] == {
        "limit": 2,
        "returned": 2,
        "total": 6,
        "truncated": True,
    }


def test_tshark_json_parser_bounds_packet_rows_before_metadata_parsing() -> None:
    stdout = _tshark_json(
        {
            "frame": {"frame.number": "1", "frame.protocols": "eth:ip:tcp", "frame.len": "1"},
            "ip": {"ip.src": "192.0.2.1", "ip.dst": "192.0.2.2"},
            "tcp": {"tcp.srcport": "1111", "tcp.dstport": "80"},
        },
        {
            "frame": {"frame.number": "2", "frame.protocols": "eth:ip:udp", "frame.len": "1"},
            "ip": {"ip.src": "192.0.2.3", "ip.dst": "192.0.2.4"},
            "udp": {"udp.srcport": "2222", "udp.dstport": "53"},
        },
        {
            "frame": {"frame.number": "3", "frame.protocols": "eth:ip:tcp:http", "frame.len": "1"},
            "ip": {"ip.src": "192.0.2.5", "ip.dst": "192.0.2.6"},
            "tcp": {"tcp.srcport": "3333", "tcp.dstport": "8080"},
        },
    )

    metadata = tshark_semantics.parse_tshark_output(stdout, "", max_rows=2)

    assert metadata["pcap"]["packet_count"] == 2
    assert metadata["hosts"] == ["192.0.2.1", "192.0.2.2"]
    assert metadata["limits"]["input_rows_truncated"] is True
    assert any("capped at max_rows=2" in warning for warning in metadata["warnings"])


def test_tshark_secret_fingerprint_requires_hmac_key(monkeypatch: pytest.MonkeyPatch) -> None:
    raw_secret = "synthetic-low-entropy-password"

    monkeypatch.delenv(tshark_semantics.SECRET_FINGERPRINT_KEY_ENV, raising=False)
    assert tshark_semantics.fingerprint_secret(raw_secret, kind="password") is None

    monkeypatch.setenv(tshark_semantics.SECRET_FINGERPRINT_KEY_ENV, "unit-test-hmac-key")
    fingerprint = tshark_semantics.fingerprint_secret(raw_secret, kind="password")

    assert fingerprint is not None
    assert fingerprint.startswith("hmac-sha256:password:")
    assert raw_secret not in fingerprint
    assert fingerprint == tshark_semantics.fingerprint_secret(raw_secret, kind="password")
    assert fingerprint != tshark_semantics.fingerprint_secret(raw_secret, kind="api_key")


def test_tshark_parser_keeps_raw_diagnostics_in_runtime_metadata() -> None:
    raw_secrets = [
        "synthetic-bearer-token",
        "synthetic-session-cookie",
        "synthetic-api-key",
        "synthetic-password",
        "synthetic-protocol-secret",
        "synthetic-private-key",
    ]
    stderr = "\n".join(
        [
            "Warning: Authorization: Bearer synthetic-bearer-token",
            "Warning: Cookie: session=synthetic-session-cookie",
            "Warning: api_key=synthetic-api-key",
            "Warning: password=synthetic-password",
            "Warning: FTP PASS synthetic-protocol-secret",
            "Warning: -----BEGIN PRIVATE KEY-----synthetic-private-key-----END PRIVATE KEY-----",
        ]
    )

    metadata = tshark_semantics.parse_tshark_output("", stderr, max_rows=20)
    serialized = str(metadata)

    assert "Authorization:" in serialized
    assert "Cookie:" in serialized
    assert "api_key=" in serialized
    assert "PASS " in serialized
    for secret in raw_secrets:
        assert secret in serialized


def test_tshark_emits_safe_semantic_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(tshark_semantics.SECRET_FINGERPRINT_KEY_ENV, "unit-test-hmac-key")
    raw_secret = "synthetic-http-observation-token"
    stdout = _tshark_json(
        {
            "frame": {
                "frame.number": "2",
                "frame.time": "Jun 14, 2026 10:00:01.500000000 UTC",
                "frame.protocols": "eth:ip:tcp:http",
                "frame.len": "512",
            },
            "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.20"},
            "tcp": {"tcp.srcport": "49152", "tcp.dstport": "80", "tcp.stream": "7"},
            "http": {
                "http.host": "app.example.test",
                "http.request.method": "GET",
                "http.request.uri": "http://app.example.test/login",
                "http.authorization": f"Bearer {raw_secret}",
            },
        }
    )
    metadata = tshark_semantics.parse_tshark_output(
        stdout,
        "",
        analysis_mode="secret_exposure",
        artifact_sha256="synthetic-pcap-sha256",
    )

    observations = TSharkTool().emit_semantic_observations(
        stdout=stdout,
        stderr="",
        exit_code=0,
        args=TSharkArgs(target="unused", analysis_mode="secret_exposure"),
        metadata=metadata,
    )

    types = {item["observation_type"] for item in observations}
    assert {
        "network.host_discovered",
        "network.service_observed",
        "network.service_detected",
        "finding.vulnerability_detected",
    }.issubset(types)
    assert any(item["subject_key"] == "host.ip:192.0.2.10" for item in observations)
    assert any(
        item["subject_key"] == "service.socket:203.0.113.20/tcp/80"
        for item in observations
    )
    passive_service = next(
        item
        for item in observations
        if item["observation_type"] == "network.service_observed"
    )
    assert passive_service["payload"]["evidence_source"] == "passive_pcap"
    assert passive_service["payload"]["reachability"] == "unverified"

    finding = next(
        item
        for item in observations
        if item["observation_type"] == "finding.vulnerability_detected"
    )
    assert finding["subject_type"] == "finding.vulnerability"
    assert finding["subject_key"].startswith(
        "finding.vulnerability:service.socket:203.0.113.20/tcp/80:tshark/"
    )
    assert finding["payload"]["finding_subtype"] == "credential_exposure_detected"
    assert finding["payload"]["frame"] == "2"
    assert finding["payload"]["stream"] == "7"
    assert finding["payload"]["proof_excerpt"].startswith("Bearer <DURABLE_SECRET_MASK:token>")
    assert finding["payload"]["pcap_artifact_sha256"] == "synthetic-pcap-sha256"

    serialized = str(observations)
    assert raw_secret not in serialized
    assert "<DURABLE_SECRET_MASK:token>" in serialized


def test_tshark_masks_bare_protocol_auth_proof_in_durable_semantics() -> None:
    raw_secret = "synthetic-ftp-password"
    stdout = _tshark_json(
        {
            "frame": {
                "frame.number": "3",
                "frame.time": "Jun 14, 2026 10:00:03.000000000 UTC",
                "frame.protocols": "eth:ip:tcp:ftp",
            },
            "ip": {"ip.src": "192.0.2.20", "ip.dst": "203.0.113.21"},
            "tcp": {"tcp.srcport": "49154", "tcp.dstport": "21", "tcp.stream": "9"},
            "ftp": {
                "ftp.request.command": "PASS",
                "ftp.request.command_parameter": raw_secret,
            },
        }
    )
    args = TSharkArgs(target="unused", analysis_mode="secret_exposure")
    metadata = tshark_semantics.parse_tshark_output(
        stdout,
        "",
        analysis_mode="secret_exposure",
        artifact_sha256="synthetic-pcap-sha256",
    )

    runtime_exposure = metadata["secret_exposure"][0]
    assert runtime_exposure["field"] == "ftp.request.command_parameter"
    assert runtime_exposure["proof_excerpt"] == raw_secret
    assert raw_secret in str(metadata)

    observations = TSharkTool().emit_semantic_observations(
        stdout=stdout,
        stderr="",
        exit_code=0,
        args=args,
        metadata=metadata,
    )
    evidence = TSharkTool().emit_semantic_evidence(
        stdout=stdout,
        stderr="",
        exit_code=0,
        args=args,
        metadata=metadata,
    )
    finding = next(
        item
        for item in observations
        if item["observation_type"] == "finding.vulnerability_detected"
    )
    packet_proof = next(item for item in evidence if item["name"] == "packet_proof")

    assert finding["payload"]["proof_excerpt"] == "<DURABLE_SECRET_MASK:secret>"
    assert "proof=<DURABLE_SECRET_MASK:secret>" in packet_proof["value"]
    assert raw_secret not in str(observations)
    assert raw_secret not in str(evidence)
    assert "ftp.request.command_parameter" in str(observations)
    assert "frame=3" in packet_proof["value"]


def test_tshark_weak_secret_signals_do_not_emit_findings() -> None:
    metadata = {
            "analysis_mode": "auth_indicators",
            "hosts": [],
            "conversations": [],
            "secret_exposure": [
                {
                    "protocol": "http",
                    "dst": "203.0.113.20",
                    "field": "http.authorization",
                    "proof_excerpt": "Bearer raw-token-without-frame",
                }
            ],
            "pcap": {"artifact_sha256": "synthetic-pcap-sha256"},
        }
    observations = tshark_semantics.build_tshark_semantic_observations(
        metadata,
        args=None,
    )

    assert [
        item
        for item in observations
        if item["observation_type"] == "finding.vulnerability_detected"
    ] == []
    assert metadata["semantic_observation_diagnostics"] == [
        {
            "reason": "missing_frame_or_stream",
            "field": "http.authorization",
            "protocol": "http",
            "frame": "",
            "stream": "",
            "source": "tshark",
        }
    ]


def test_tshark_emits_bounded_semantic_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(tshark_semantics.SECRET_FINGERPRINT_KEY_ENV, "unit-test-hmac-key")
    raw_secret = "synthetic-evidence-token"
    stdout = _tshark_json(
        {
            "frame": {
                "frame.number": "2",
                "frame.time": "Jun 14, 2026 10:00:01.500000000 UTC",
                "frame.protocols": "eth:ip:tcp:http",
                "frame.len": "512",
            },
            "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.20"},
            "tcp": {"tcp.srcport": "49152", "tcp.dstport": "80", "tcp.stream": "7"},
            "http": {
                "http.host": "app.example.test",
                "http.request.method": "GET",
                "http.request.uri": "http://app.example.test/login",
                "http.authorization": f"Bearer {raw_secret}",
            },
        }
    )
    metadata = tshark_semantics.parse_tshark_output(
        stdout,
        "Warning: TShark output truncated by fixture",
        analysis_mode="secret_exposure",
        artifact_sha256="synthetic-pcap-sha256",
        max_rows=1,
    )
    metadata["limits"]["truncated"] = True
    args = TSharkArgs(
        target="unused",
        analysis_mode="secret_exposure",
        input_file="captures/example.pcap",
        display_filter="http.authorization",
        max_rows=1,
    )

    evidence = TSharkTool().emit_semantic_evidence(
        stdout=stdout,
        stderr="",
        exit_code=0,
        args=args,
        metadata=metadata,
    )
    valid, dropped = validate_semantic_evidence_entries(evidence)

    assert dropped == []
    assert {entry["type"] for entry in valid} >= {
        SemanticEvidenceType.VARIANT.value,
        SemanticEvidenceType.EXECUTION_PARAMETER.value,
        SemanticEvidenceType.RESULT_SUMMARY.value,
        SemanticEvidenceType.DIAGNOSTIC.value,
    }
    assert any(
        entry["name"] == "analysis_mode"
        and entry["value"] == "find_security_relevant_artifacts"
        for entry in valid
    )
    assert any(entry["name"] == "input_file_mode" and entry["value"] == "pcap_file" for entry in valid)
    assert any(entry["name"] == "max_rows" and entry["value"] == 1 for entry in valid)
    assert any(entry["name"] == "packet_count" and entry["value"] == 1 for entry in valid)
    assert any(entry["name"] == "secret_exposure_count" and entry["value"] == 1 for entry in valid)

    proof = next(entry for entry in valid if entry["name"] == "packet_proof")
    assert proof["detail"] == {"severity": "info", "note": "secret_exposure"}
    assert "frame=2" in proof["value"]
    assert "field=http.authorization" in proof["value"]
    assert "<DURABLE_SECRET_MASK:token>" in proof["value"]
    assert raw_secret not in str(valid)


def test_tshark_semantic_evidence_builder_respects_validator_caps() -> None:
    metadata = {
        "analysis_mode": "field_extract",
        "pcap": {"packet_count": 4},
        "conversations": [{"flow_key": str(index)} for index in range(3)],
        "secret_exposure": [
            {
                "frame": str(index + 1),
                "stream": str(index),
                "protocol": "http",
                "dst": "203.0.113.20",
                "field": "http.authorization",
                "flow_key": "tcp:192.0.2.10:49152->203.0.113.20:80",
                "proof_excerpt": "Bearer synthetic-proof-token",
            }
            for index in range(10)
        ],
        "limits": {
            "max_rows": 2,
            "truncated": True,
            "lists": {"secret_exposure": {"truncated": True}},
        },
        "warnings": [
            "TShark field is not allowlisted: tcp.payload",
            "unsupported fixture field",
        ],
    }
    args = TSharkArgs(
        target="unused",
        analysis_mode="field_extract",
        fields=["frame.number", "ip.src"],
        display_filter="frame.number >= 1",
        max_rows=2,
    )

    evidence = tshark_semantics.build_tshark_semantic_evidence(metadata, args)
    valid, dropped = validate_semantic_evidence_entries(evidence)
    diagnostic_entries = [
        entry for entry in valid if entry["type"] == SemanticEvidenceType.DIAGNOSTIC.value
    ]

    assert dropped == []
    assert len(diagnostic_entries) <= 4
    assert {entry["name"] for entry in diagnostic_entries} == {
        "truncated_output",
        "unsupported_fields",
        "packet_proof",
    }
    assert "tcp.payload" in str(diagnostic_entries)
    assert "synthetic" not in str(valid).lower()


def test_tshark_run_returns_compact_stdout_and_keeps_runtime_proof_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    (tmp_path / "captures").mkdir()
    (tmp_path / "captures" / "example.pcap").write_bytes(b"pcap")

    raw_secrets = [
        "synthetic-bearer-token",
        "synthetic-session-secret",
        "synthetic-password",
        "synthetic-api-token",
    ]
    stdout = _tshark_json(
        {
            "frame": {
                "frame.number": "1",
                "frame.time": "Jun 14, 2026 10:00:00.000000000 UTC",
                "frame.protocols": "eth:ip:tcp:http",
                "frame.len": "512",
            },
            "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.20"},
            "tcp": {"tcp.srcport": "49152", "tcp.dstport": "80", "tcp.stream": "7"},
            "http": {
                "http.host": "app.example.test",
                "http.request.method": "GET",
                "http.request.uri": "http://app.example.test/login",
                "http.authorization": "Bearer synthetic-bearer-token",
                "http.cookie": "session=synthetic-session-secret",
            },
        }
    )
    stderr = "\n".join(
        [
            "Warning: password=synthetic-password",
            "Warning: X-Api-Key: synthetic-api-token",
        ]
    )

    def fake_run(command: list[str], **kwargs):
        return tshark_module.subprocess.CompletedProcess(command, 0, stdout, stderr)

    monkeypatch.setattr(tshark_module.subprocess, "run", fake_run)

    result = TSharkTool().run(
            TSharkArgs(
                target="unused",
                input_file="captures/example.pcap",
                analysis_mode="secret_exposure",
            )
    )

    assert result.success is True
    assert result.metadata["pcap"]["artifact_sha256"] == sha256(b"pcap").hexdigest()
    assert result.metadata["secret_exposure"][0]["pcap_artifact_sha256"] == sha256(b"pcap").hexdigest()
    assert result.metadata["pcap_compact"]["schema_version"] == "pcap.compact.v1"
    assert result.metadata["compact_key_findings"]
    assert result.metadata["compact_decision_evidence"]
    assert result.artifacts == []
    compact_stdout = json.loads(result.stdout)
    assert compact_stdout["schema_version"] == "pcap.compact.v1"
    assert compact_stdout["source_tool"] == "sniffing_spoofing.network_sniffers.tshark"
    assert "_source" not in result.stdout
    assert "frame.protocols" not in result.stdout
    serialized = "\n".join(
        [
            result.stdout,
            result.stderr,
            str(result.metadata),
            "\n".join(
                (tmp_path / artifact).read_text(encoding="utf-8")
                for artifact in result.artifacts
            ),
        ]
    )
    assert "http.authorization" in result.stdout
    for secret in raw_secrets:
        assert secret in serialized


def test_tshark_tool_parse_output_honors_sensitive_proof_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(tshark_semantics.SECRET_FINGERPRINT_KEY_ENV, "unit-test-hmac-key")
    raw_secret = "synthetic-proof-mode-token"
    stdout = _tshark_json(
        {
            "frame": {
                "frame.number": "2",
                "frame.time": "Jun 14, 2026 10:00:01.500000000 UTC",
                "frame.protocols": "eth:ip:tcp:http",
            },
            "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.20"},
            "tcp": {"tcp.srcport": "49152", "tcp.dstport": "80", "tcp.stream": "7"},
            "http": {"http.authorization": f"Bearer {raw_secret}"},
        }
    )
    tool = TSharkTool()

    metadata_only = tool.parse_output(
        stdout,
        "",
        0,
        TSharkArgs(
            target="unused",
            analysis_mode="secret_exposure",
            sensitive_proof_mode="metadata_only",
        ),
    )
    proof_excerpt = tool.parse_output(
        stdout,
        "",
        0,
        TSharkArgs(
            target="unused",
            analysis_mode="secret_exposure",
            sensitive_proof_mode="proof_excerpt",
        ),
    )
    fingerprint = tool.parse_output(
        stdout,
        "",
        0,
        TSharkArgs(
            target="unused",
            analysis_mode="secret_exposure",
            sensitive_proof_mode="fingerprint",
        ),
    )

    metadata_only_exposure = metadata_only["secret_exposure"][0]
    proof_excerpt_exposure = proof_excerpt["secret_exposure"][0]
    fingerprint_exposure = fingerprint["secret_exposure"][0]

    assert metadata_only_exposure["proof_mode"] == "metadata_only"
    assert "proof_excerpt" not in metadata_only_exposure
    assert "fingerprint" not in metadata_only_exposure
    assert proof_excerpt_exposure["proof_mode"] == "proof_excerpt"
    assert proof_excerpt_exposure["proof_excerpt"] == f"Bearer {raw_secret}"
    assert "fingerprint" not in proof_excerpt_exposure
    assert fingerprint_exposure["proof_mode"] == "fingerprint"
    assert fingerprint_exposure["fingerprint"].startswith("hmac-sha256:authorization_header:")
    assert "proof_excerpt" not in fingerprint_exposure
    assert raw_secret in str(proof_excerpt)
    assert raw_secret not in str([metadata_only, fingerprint])


def test_tshark_repeated_secret_exposure_keeps_raw_runtime_proof_and_masks_semantics() -> None:
    raw_secret = "synthetic-runtime-proof-token"
    stdout = _tshark_json(
        {
            "frame": {
                "frame.number": "1",
                "frame.time": "Jun 14, 2026 10:00:01.000000000 UTC",
                "frame.protocols": "eth:ip:tcp:http",
            },
            "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.20"},
            "tcp": {"tcp.srcport": "49152", "tcp.dstport": "80", "tcp.stream": "7"},
            "http": {"http.authorization": f"Bearer {raw_secret}"},
        },
        {
            "frame": {
                "frame.number": "2",
                "frame.time": "Jun 14, 2026 10:00:02.000000000 UTC",
                "frame.protocols": "eth:ip:tcp:http",
            },
            "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.20"},
            "tcp": {"tcp.srcport": "49152", "tcp.dstport": "80", "tcp.stream": "7"},
            "http": {"http.authorization": f"Bearer {raw_secret}"},
        },
    )
    metadata = TSharkTool().parse_output(
        stdout,
        "",
        0,
        TSharkArgs(target="unused", analysis_mode="secret_exposure"),
    )

    exposures = metadata["secret_exposure"]

    assert len(exposures) == 2
    assert all(item["proof_mode"] == "proof_excerpt" for item in exposures)
    assert all(item["proof_excerpt"] == f"Bearer {raw_secret}" for item in exposures)
    assert raw_secret in str(metadata)

    observations = TSharkTool().emit_semantic_observations(
        stdout=stdout,
        stderr="",
        exit_code=0,
        args=TSharkArgs(target="unused", analysis_mode="secret_exposure"),
        metadata=metadata,
    )
    finding = next(
        item
        for item in observations
        if item["observation_type"] == "finding.vulnerability_detected"
    )
    assert finding["payload"]["proof_mode"] == "proof_excerpt"
    assert finding["payload"]["proof_excerpt"] == "Bearer <DURABLE_SECRET_MASK:token>"
    assert raw_secret not in str(observations)


def test_tshark_runtime_scope_does_not_mask_generic_hash_values() -> None:
    digest = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    stdout = _tshark_json(
        {
            "frame": {
                "frame.number": "1",
                "frame.protocols": "eth:ip:tcp:tls",
            },
            "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.20"},
            "tcp": {"tcp.srcport": "49152", "tcp.dstport": "443", "tcp.stream": "7"},
            "tls": {
                "tls.handshake.extensions_server_name": "secure.example.test",
                "tls.handshake.version": "0x0303",
            },
            "x509sat": {"x509sat.issuer": digest},
        }
    )
    metadata = TSharkTool().parse_output(
        stdout,
        "",
        0,
        TSharkArgs(target="unused", analysis_mode="tls"),
    )

    assert metadata["tls"][0]["issuer"] == digest


def test_tshark_concrete_flags_do_not_leak_into_backend() -> None:
    backend_root = Path(__file__).resolve().parents[2] / "backend"
    leaked: list[str] = []

    for path in backend_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        if "tshark" not in lowered:
            continue
        if any(flag in text for flag in ('"-T"', "'-T'", '"-r"', "'-r'", '"-w"', "'-w'")):
            leaked.append(str(path.relative_to(backend_root.parent)))

    assert leaked == []


def test_tshark_planner_schema_hides_runtime_controls() -> None:
    schema = TSharkTool.get_planner_args_model().model_json_schema()
    properties = set(schema.get("properties", {}))

    assert {
        "analysis_mode",
        "input_file",
        "interface",
        "display_filter",
        "capture_filter",
        "host",
        "port",
        "protocol",
        "fields",
        "include_payload_indicators",
        "max_rows",
        "sensitive_proof_mode",
    }.issubset(properties)
    assert {
        "target",
        "extra_args",
        "timeout",
        "packet_count",
        "duration_seconds",
        "output_file",
    }.isdisjoint(properties)

    with pytest.raises(ValidationError):
        TSharkPlannerArgs(analysis_mode="pcap_summary", extra_args=["-V"])


def test_tshark_planner_validation_enforces_closed_contract() -> None:
    with pytest.raises(ValidationError):
        TSharkPlannerArgs(
            analysis_mode="pcap_summary",
            sensitive_proof_mode="raw_secret",
        )

    with pytest.raises(ValidationError):
        TSharkPlannerArgs(analysis_mode="pcap_summary", max_rows=1_001)

    with pytest.raises(ValidationError):
        TSharkPlannerArgs(
            analysis_mode="pcap_summary",
            fields=["frame.number"],
        )

    with pytest.raises(ValidationError):
        TSharkPlannerArgs(analysis_mode="field_extract")

    with pytest.raises(ValidationError):
        TSharkPlannerArgs(
            analysis_mode="field_extract",
            fields=["frame.number", "tcp.payload"],
        )

    with pytest.raises(ValidationError):
        TSharkPlannerArgs(
            analysis_mode="field_extract",
            fields=["frame.number", "../bad"],
        )

    with pytest.raises(ValidationError):
        TSharkPlannerArgs(
            analysis_mode="pcap_summary",
            input_file="captures/example.pcap",
            capture_filter="tcp port 80",
        )

    args = TSharkPlannerArgs(
        analysis_mode="field_extract",
        fields=["frame.number", "ip.src"],
        display_filter="frame.number >= 1",
        include_payload_indicators=True,
        max_rows=1_000,
        sensitive_proof_mode="fingerprint",
    )
    assert args.analysis_mode == TSharkAnalysisMode.EXTRACT_EVIDENCE
    assert args.fields == ["frame.number", "ip.src"]


def test_tshark_direct_args_reject_non_allowlisted_field_extract_fields() -> None:
    with pytest.raises(ValidationError):
        TSharkArgs(
            target="unused",
            analysis_mode="field_extract",
            fields=["frame.number", "tcp.payload"],
        )


def test_tshark_direct_args_reject_runtime_flag_escape_hatches() -> None:
    for payload in (
        {"extra_args": ["-V"]},
        {"output_file": "captures/out.pcap"},
        {"output_format": "pdml"},
        {"verbose": True},
    ):
        with pytest.raises(ValidationError):
            TSharkArgs(target="unused", **payload)


def test_tshark_direct_args_reject_offline_capture_filter() -> None:
    with pytest.raises(ValidationError):
        TSharkArgs(
            target="unused",
            input_file="captures/example.pcap",
            capture_filter="tcp port 80",
        )


def test_tshark_planner_compilation_injects_runtime_target() -> None:
    offline = TSharkTool.compile_planner_parameters(
        {
            "analysis_mode": "pcap_summary",
            "input_file": "captures/example.pcap",
            "max_rows": 50,
        },
        action_target=None,
    )
    live = TSharkTool.compile_planner_parameters(
        {
            "analysis_mode": "http",
            "interface": "eth0",
            "display_filter": "http",
        },
        action_target=None,
    )
    targeted = TSharkTool.compile_planner_parameters(
        {"analysis_mode": "dns"},
        action_target="192.0.2.10",
    )

    assert offline["target"] == TSHARK_SAFE_TARGET_PLACEHOLDER
    assert live["target"] == TSHARK_SAFE_TARGET_PLACEHOLDER
    assert targeted["target"] == "192.0.2.10"
    assert {
        "extra_args",
        "timeout",
        "packet_count",
        "duration_seconds",
        "output_format",
        "output_file",
    }.isdisjoint(offline)


def test_tshark_planner_scope_fields_compile_to_display_filter() -> None:
    compiled = TSharkTool.compile_planner_parameters(
        {
            "analysis_mode": "http",
            "input_file": "captures/example.pcap",
            "display_filter": "http",
            "host": "203.0.113.20",
            "port": 443,
            "protocol": "tls",
        }
    )

    assert compiled["display_filter"] == "http"
    assert compiled["host"] == "203.0.113.20"
    assert compiled["port"] == 443
    assert compiled["protocol"] == "tls"


def test_tshark_planner_compiled_commands_use_fields_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    (tmp_path / "captures").mkdir()
    (tmp_path / "captures" / "example.pcap").write_bytes(b"pcap")

    offline_params = TSharkTool.compile_planner_parameters(
        {
            "analysis_mode": "pcap_summary",
            "input_file": "captures/example.pcap",
        }
    )
    live_params = TSharkTool.compile_planner_parameters(
        {
            "analysis_mode": "dns",
            "interface": "eth0",
            "display_filter": "dns",
        }
    )

    offline_command = TSharkTool().build_command(TSharkArgs(**offline_params))
    live_command = TSharkTool().build_command(TSharkArgs(**live_params))

    assert _flag_value(offline_command, "-T") == "fields"
    assert _flag_value(offline_command, "-c") == "100"
    assert _flag_value(live_command, "-T") == "fields"


def test_tshark_planner_compiled_field_extract_uses_fields_mode() -> None:
    params = TSharkTool.compile_planner_parameters(
            {
                "analysis_mode": "field_extract",
                "fields": ["frame.number", "ip.src"],
                "display_filter": "frame.number >= 1",
            }
        )

    command = TSharkTool().build_command(TSharkArgs(**params))

    assert _flag_value(command, "-T") == "fields"
    assert command.count("-e") == 2
    assert command[command.index("-e") + 1] == "frame.number"


def test_tshark_pcap_paths_are_workspace_resolved(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("WORKSPACE", str(tmp_path))
    (tmp_path / "captures").mkdir()
    (tmp_path / "captures" / "example.pcap").write_bytes(b"pcap")

    input_command = TSharkTool().build_command(
        TSharkArgs(target="unused", input_file="captures/example.pcap")
    )

    assert _flag_value(input_command, "-r") == "/workspace/captures/example.pcap"

    with pytest.raises(ValueError):
        TSharkTool().build_command(TSharkArgs(target="unused", input_file="/tmp/escape.pcap"))

    with pytest.raises(ValidationError):
        TSharkArgs(target="unused", output_file="../escape.pcap")


def test_tshark_live_capture_command_is_bounded_by_default() -> None:
    command = TSharkTool().build_command(TSharkArgs(target="unused", interface="eth0"))

    assert command[:3] == ["timeout", f"{TSHARK_HARD_TIMEOUT_SECONDS}s", "tshark"]
    assert _flag_value(command, "-i") == "eth0"
    assert _flag_value(command, "-s") == str(TSHARK_DEFAULT_SNAPLEN)
    assert _flag_value(command, "-c") == "100"
    assert _flag_value(command, "-a") == f"duration:{TSHARK_HARD_TIMEOUT_SECONDS}"


def test_tshark_direct_live_packet_count_is_clamped() -> None:
    command = TSharkTool().build_command(
        TSharkArgs(target="unused", packet_count=1_000_000, duration_seconds=3_600)
    )

    assert _flag_value(command, "-c") == str(TSHARK_LIVE_PACKET_LIMIT)
    assert _flag_value(command, "-a") == f"duration:{TSHARK_HARD_TIMEOUT_SECONDS}"


def test_tshark_generated_live_command_passes_validator() -> None:
    command = TSharkTool().build_command(TSharkArgs(target="unused", capture_filter="tcp"))
    result = CommandValidator().validate_command(
        command,
        "sniffing_spoofing.network_sniffers.tshark",
    )

    assert result.valid, result.errors


def test_tshark_bounded_timeout_requires_usable_output() -> None:
    tool = TSharkTool()
    args = TSharkArgs(target="unused")

    usable_metadata = tool.parse_output(
        "1 0.000000 192.0.2.1 -> 192.0.2.2 TCP",
        "",
        TSHARK_TIMEOUT_EXIT_CODE,
        args,
    )
    empty_metadata = tool.parse_output("", "", TSHARK_TIMEOUT_EXIT_CODE, args)

    assert usable_metadata["execution_outcome"] == "informational"
    assert tool.is_success_exit_code(
        TSHARK_TIMEOUT_EXIT_CODE,
        args,
        stdout="1 0.000000 192.0.2.1 -> 192.0.2.2 TCP",
        parsed_metadata=usable_metadata,
    )
    assert empty_metadata["execution_outcome"] == "failed"
    assert not tool.is_success_exit_code(
        TSHARK_TIMEOUT_EXIT_CODE,
        args,
        parsed_metadata=empty_metadata,
    )
