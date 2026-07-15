"""Tests for deterministic PCAP compact-envelope construction."""

from __future__ import annotations

from agent.tools.pcap_compaction import PCAP_COMPACT_SCHEMA_VERSION, build_pcap_compaction


def test_pcap_compaction_empty_metadata_is_stable() -> None:
    first = build_pcap_compaction({}, source_tool="unit.pcap")
    second = build_pcap_compaction({}, source_tool="unit.pcap")

    assert first == second
    compact = first["pcap_compact"]
    assert compact["schema_version"] == PCAP_COMPACT_SCHEMA_VERSION
    assert compact["pcap"]["packet_count"] == 0
    assert compact["summary_counts"]["secret_exposure"] == 0
    assert first["compact_summary"] == "PCAP compact analysis parsed 0 packets, 0 hosts, 0 conversations."
    assert first["compact_key_findings"] == ["PCAP analysis parsed 0 packets."]


def test_pcap_compaction_prioritizes_secret_and_auth_evidence() -> None:
    metadata = {
        "analysis_mode": "secret_exposure",
        "output_format": "json",
        "pcap": {"packet_count": 2, "duration_seconds": 1.5},
        "secret_exposure": [
            {
                "frame": "2",
                "stream": "7",
                "protocol": "http",
                "src": "192.0.2.10",
                "dst": "203.0.113.20",
                "field": "http.authorization",
                "kind": "authorization_header",
                "proof_mode": "proof_excerpt",
                "proof_excerpt": "Bearer raw-runtime-token",
                "extraction_filter": "http.authorization",
            }
        ],
        "auth_indicators": [
            {
                "frame": "2",
                "field": "http.authorization",
                "mechanism": "bearer_token",
                "value": "Bearer raw-runtime-token",
            }
        ],
        "tls": [{"frame": "1", "sni": "secure.example.test", "stream": "4"}],
        "limits": {"max_rows": 100, "truncated": False, "lists": {}},
    }

    result = build_pcap_compaction(metadata, source_tool="unit.pcap")
    compact = result["pcap_compact"]

    assert result["compact_key_findings"][0].startswith("Secret exposure:")
    assert "raw-runtime-token" in str(compact["security_signals"])
    assert compact["next_pivots"][0] == {
        "reason": "secret_exposure",
        "display_filter": "http.authorization",
    }
    assert "raw-runtime-token" in str(result["compact_decision_evidence"])


def test_pcap_compaction_prioritizes_credential_events_and_sequences() -> None:
    metadata = {
        "analysis_mode": "pcap_summary",
        "output_format": "json",
        "pcap": {"packet_count": 3},
        "credential_events": [
            {
                "frame": "40",
                "stream": "3",
                "protocol": "ftp",
                "src": "192.168.196.1",
                "dst": "192.168.196.16",
                "field": "ftp.request.arg",
                "kind": "password",
                "role": "password",
                "proof_mode": "proof_excerpt",
                "proof_excerpt": "Buck3tH4TF0RM3!",
                "command": "PASS",
                "extraction_filter": "ftp.request.command == PASS",
            }
        ],
        "auth_sequences": [
            {
                "stream": "3",
                "flow_key": "tcp:192.168.196.1:54411->192.168.196.16:21",
                "protocol": "ftp",
                "frames": ["36", "40", "42"],
                "username_count": 1,
                "secret_count": 1,
                "success_count": 1,
            }
        ],
        "conversations": [
            {"src": "192.168.196.1", "dst": "192.168.196.16", "protocol": "tcp"}
        ],
        "limits": {"max_rows": 100, "truncated": False, "lists": {}},
    }

    result = build_pcap_compaction(metadata, source_tool="unit.pcap")
    compact = result["pcap_compact"]

    assert compact["summary_counts"]["credential_events"] == 1
    assert compact["summary_counts"]["auth_sequences"] == 1
    assert compact["security_signals"]["credential_events"]["returned"] == 1
    assert result["compact_key_findings"][0].startswith("Credential event:")
    assert "Buck3tH4TF0RM3!" in str(result["compact_decision_evidence"])
    assert compact["next_pivots"][0] == {
        "reason": "credential_event",
        "display_filter": "ftp.request.command == PASS",
    }


def test_pcap_compaction_promotes_ftp_protocol_evidence() -> None:
    metadata = {
        "analysis_mode": "extract_evidence",
        "output_format": "fields",
        "pcap": {"packet_count": 4},
        "ftp": [
            {
                "frame": "34",
                "stream": "3",
                "src": "192.168.196.16",
                "dst": "192.168.196.1",
                "response_code": "220",
                "response_arg": "(vsFTPd 3.0.3)",
            },
            {
                "frame": "36",
                "stream": "3",
                "src": "192.168.196.1",
                "dst": "192.168.196.16",
                "request_command": "USER",
                "request_arg": "nathan",
            },
        ],
        "field_extract": [
            {"row": 1, "fields": {"frame.number": "34", "ftp.response.code": "220"}}
        ],
        "limits": {"max_rows": 100, "truncated": False, "lists": {}},
    }

    result = build_pcap_compaction(metadata, source_tool="unit.pcap")
    compact = result["pcap_compact"]

    assert compact["summary_counts"]["ftp"] == 2
    assert compact["protocol_evidence"]["ftp"]["returned"] == 2
    assert any(finding.startswith("FTP evidence:") for finding in result["compact_key_findings"])
    assert "Running as user" not in str(compact)


def test_pcap_compaction_surfaces_truncation_and_omissions() -> None:
    metadata = {
        "analysis_mode": "pcap_summary",
        "pcap": {"packet_count": 5},
        "conversations": [
            {"src": f"192.0.2.{index}", "dst": "203.0.113.20", "protocol": "tcp"}
            for index in range(12)
        ],
        "limits": {
            "max_rows": 5,
            "truncated": True,
            "input_rows_truncated": True,
            "lists": {
                "conversations": {
                    "limit": 5,
                    "returned": 12,
                    "total": 12,
                    "truncated": True,
                }
            },
        },
    }

    compact = build_pcap_compaction(metadata, source_tool="unit.pcap")["pcap_compact"]

    assert compact["coverage"]["truncated"] is True
    assert compact["coverage"]["input_rows_truncated"] is True
    assert compact["flows"]["returned"] == 10
    assert compact["flows"]["omitted"] == 2
    assert compact["omissions"]["conversations"]["omitted"] == 2
    assert "dns" in compact["omissions"]["not_analyzed_by_mode"]
