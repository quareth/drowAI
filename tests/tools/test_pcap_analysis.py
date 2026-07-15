"""Tests for reusable decoded-PCAP critical signal extraction."""

from __future__ import annotations

from agent.tools.pcap_analysis import extract_critical_signals, flatten_tshark_packets


def _packet(layers: dict) -> dict:
    return {"_source": {"layers": layers}}


def test_flatten_tshark_packets_handles_nested_ftp_command_objects() -> None:
    rows = [
        _packet(
            {
                "frame": {"frame.number": "40", "frame.protocols": "eth:ip:tcp:ftp"},
                "ip": {"ip.src": "192.168.196.1", "ip.dst": "192.168.196.16"},
                "tcp": {"tcp.srcport": "54411", "tcp.dstport": "21", "tcp.stream": "3"},
                "ftp": {
                    "PASS Buck3tH4TF0RM3!\r\n": {
                        "ftp.request.command": "PASS",
                        "ftp.request.arg": "Buck3tH4TF0RM3!",
                    }
                },
            }
        )
    ]

    fields = flatten_tshark_packets(rows)

    assert any(field.field_name == "ftp.request.command" and field.value == "PASS" for field in fields)
    password = next(field for field in fields if field.field_name == "ftp.request.arg")
    assert password.value == "Buck3tH4TF0RM3!"
    assert password.context.stream == "3"
    assert password.context.app_protocol == "ftp"


def test_extract_critical_signals_classifies_and_correlates_ftp_credentials() -> None:
    rows = [
        _packet(
            {
                "frame": {"frame.number": "36", "frame.protocols": "eth:ip:tcp:ftp"},
                "ip": {"ip.src": "192.168.196.1", "ip.dst": "192.168.196.16"},
                "tcp": {"tcp.srcport": "54411", "tcp.dstport": "21", "tcp.stream": "3"},
                "ftp": {
                    "USER nathan\r\n": {
                        "ftp.request.command": "USER",
                        "ftp.request.arg": "nathan",
                    }
                },
            }
        ),
        _packet(
            {
                "frame": {"frame.number": "40", "frame.protocols": "eth:ip:tcp:ftp"},
                "ip": {"ip.src": "192.168.196.1", "ip.dst": "192.168.196.16"},
                "tcp": {"tcp.srcport": "54411", "tcp.dstport": "21", "tcp.stream": "3"},
                "ftp": {
                    "PASS Buck3tH4TF0RM3!\r\n": {
                        "ftp.request.command": "PASS",
                        "ftp.request.arg": "Buck3tH4TF0RM3!",
                    }
                },
            }
        ),
        _packet(
            {
                "frame": {"frame.number": "42", "frame.protocols": "eth:ip:tcp:ftp"},
                "ip": {"ip.src": "192.168.196.16", "ip.dst": "192.168.196.1"},
                "tcp": {"tcp.srcport": "21", "tcp.dstport": "54411", "tcp.stream": "3"},
                "ftp": {
                    "230 Login successful.\r\n": {
                        "ftp.response.code": "230",
                        "ftp.response.arg": "Login successful.",
                    }
                },
            }
        ),
    ]

    signals = extract_critical_signals(rows)
    events = signals["credential_events"]
    sequences = signals["auth_sequences"]

    assert any(event["role"] == "username" and event["value"] == "nathan" for event in events)
    assert any(event["role"] == "password" and event["value"] == "Buck3tH4TF0RM3!" for event in events)
    assert sequences
    assert sequences[0]["stream"] == "3"
    assert sequences[0]["username_count"] == 1
    assert sequences[0]["secret_count"] == 1
    assert sequences[0]["success_count"] == 1


def test_extract_critical_signals_detects_common_header_and_key_values() -> None:
    rows = [
        _packet(
            {
                "frame": {"frame.number": "1", "frame.protocols": "eth:ip:tcp:http"},
                "ip": {"ip.src": "192.0.2.10", "ip.dst": "203.0.113.20"},
                "tcp": {"tcp.srcport": "49152", "tcp.dstport": "80", "tcp.stream": "7"},
                "http": {
                    "http.authorization": "Bearer synthetic-token",
                    "http.cookie": "session=synthetic-cookie",
                    "http.file_data": "api_key=synthetic-api-key password=synthetic-password",
                },
            }
        )
    ]

    events = extract_critical_signals(rows)["credential_events"]
    kinds = {event["kind"] for event in events}

    assert {"authorization_header", "cookie", "secret"}.issubset(kinds)
