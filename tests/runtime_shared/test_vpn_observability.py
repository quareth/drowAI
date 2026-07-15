"""Tests for provider-neutral VPN failure and terminal-log normalization."""

from runtime_shared.vpn_observability import (
    classify_vpn_failure,
    find_vpn_route_overlap,
    normalize_vpn_log_lines,
    parse_vpn_status_output,
)


def test_status_parser_accepts_manager_json_and_legacy_sentinel() -> None:
    assert parse_vpn_status_output(
        'wrapper\n{"status":"reconnecting","ip_address":"","error_message":""}'
    ) == {"status": "reconnecting", "ip_address": None, "error_message": None}
    assert parse_vpn_status_output("__DROWAI_VPN_STATUS__=connected|10.8.0.2") == {
        "status": "connected",
        "ip_address": "10.8.0.2",
        "error_message": None,
    }


def test_vpn_logs_are_classified_and_sensitive_values_are_redacted() -> None:
    entries = normalize_vpn_log_lines(
        [
            "2026-07-12 14:50:24 TLS Error: TLS handshake failed",
            "[2026-07-12T14:50:25+0000] Restart pause, 2 second(s)",
            "password=secret-value",
        ]
    )

    assert [entry["level"] for entry in entries] == ["error", "warning", "info"]
    assert entries[0]["service"] == "vpn"
    assert entries[0]["timestamp"] == "2026-07-12T14:50:24Z"
    assert entries[1]["timestamp"] == "2026-07-12T14:50:25Z"
    assert entries[2]["message"] == "password=<REDACTED>"


def test_vpn_logs_normalize_aware_timestamps_to_utc() -> None:
    entries = normalize_vpn_log_lines(
        ["[2026-07-12T17:50:25+0300] connected", "unparseable message"]
    )

    assert entries[0]["timestamp"] == "2026-07-12T14:50:25Z"
    assert entries[1]["timestamp"] == ""


def test_failure_classifier_returns_only_fixed_sanitized_categories() -> None:
    assert classify_vpn_failure(
        ["RESOLVE: Cannot resolve host address: secret.example:443"]
    ) == {
        "category": "dns_resolution",
        "message": "VPN DNS resolution failed",
    }
    assert classify_vpn_failure(["AUTH_FAILED, password=super-secret"]) == {
        "category": "authentication",
        "message": "VPN authentication failed",
    }
    assert classify_vpn_failure(["TLS Error: TLS key negotiation failed"]) == {
        "category": "tls_negotiation",
        "message": "VPN TLS negotiation failed",
    }
    assert classify_vpn_failure(["ERROR: Cannot open TUN/TAP dev /dev/net/tun"]) == {
        "category": "device",
        "message": "VPN tunnel device setup failed",
    }
    assert classify_vpn_failure(["ERROR: Linux route add command failed"]) == {
        "category": "route",
        "message": "VPN route setup failed",
    }
    assert classify_vpn_failure(["Options error: unsupported directive"]) == {
        "category": "config",
        "message": "VPN configuration is unavailable",
    }
    assert classify_vpn_failure(["password=super-secret"]) == {
        "category": "process_exit",
        "message": "OpenVPN process exited before the tunnel was ready",
    }
    assert classify_vpn_failure(
        ["RESOLVE: Cannot resolve host address: stale.example", "AUTH_FAILED"]
    ) == {
        "category": "authentication",
        "message": "VPN authentication failed",
    }
    assert classify_vpn_failure(
        [
            "RESOLVE: Cannot resolve host address: stale.example",
            "[2026-07-12T14:50:25Z] Starting OpenVPN attempt 123",
        ],
        fallback_category="process_start",
    ) == {
        "category": "process_start",
        "message": "OpenVPN process failed to start",
    }


def test_route_overlap_ignores_default_and_reports_first_overlap() -> None:
    assert (
        find_vpn_route_overlap(
            "198.18.0.8/29", ["default", "10.0.0.0/8", "198.18.0.12/30"]
        )
        == "198.18.0.12/30"
    )
    assert find_vpn_route_overlap("198.18.0.8/29", ["10.0.0.0/8"]) is None
