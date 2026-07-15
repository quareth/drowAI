"""Provider-neutral VPN status and log normalization contracts.

This module converts runtime VPN manager output into transport-safe status
snapshots and Docker Terminal log rows. It contains no backend, runner, or
container lifecycle dependencies.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import sys
from typing import Any, Iterable

VPN_CONNECTION_STATES = frozenset(
    {"configured", "connecting", "reconnecting", "connected", "failed", "disconnected"}
)

_ERROR_MARKERS = (
    "auth_failed",
    "tls error",
    "options error",
    "cannot ",
    "failed",
    "deadline exceeded",
    "unavailable",
)
_WARNING_MARKERS = ("restart pause", "restarting", "inactivity timeout", "unhealthy")
_SENSITIVE_PATTERN = re.compile(
    r"(?i)(auth-user-pass|password|token|private[_ -]?key|authorization)(\s*[=:]\s*)(\S+)"
)

VPN_FAILURE_MESSAGES = {
    "dns_resolution": "VPN DNS resolution failed",
    "authentication": "VPN authentication failed",
    "tls_negotiation": "VPN TLS negotiation failed",
    "device": "VPN tunnel device setup failed",
    "route": "VPN route setup failed",
    "process_start": "OpenVPN process failed to start",
    "process_stop": "OpenVPN process could not be stopped safely",
    "process_exit": "OpenVPN process exited before the tunnel was ready",
    "deadline": "VPN connection deadline exceeded",
    "config": "VPN configuration is unavailable",
}

_FAILURE_PATTERNS = (
    ("config", ("options error", "unrecognized option", "error opening configuration")),
    (
        "dns_resolution",
        (
            "cannot resolve host address",
            "temporary failure in name resolution",
            "name or service not known",
        ),
    ),
    ("authentication", ("auth_failed", "authentication failed", "auth failure")),
    ("tls_negotiation", ("tls error", "tls key negotiation", "tls handshake")),
    ("device", ("cannot open tun", "cannot open tap", "tunsettiff", "/dev/net/tun")),
    (
        "route",
        (
            "route add command failed",
            "route addition failed",
            "net_route_v4_add",
            "error adding route",
        ),
    ),
)


def parse_vpn_status_output(output: object) -> dict[str, Any] | None:
    """Parse the last manager JSON object from command output."""
    if isinstance(output, bytes):
        text = output.decode("utf-8", errors="replace")
    else:
        text = str(output or "")
    for raw_line in reversed(text.splitlines()):
        line = raw_line.strip()
        if line.startswith("__DROWAI_VPN_STATUS__="):
            status_payload = line.partition("=")[2]
            status, _, ip_address = status_payload.partition("|")
            normalized_status = status.strip().lower()
            if normalized_status in VPN_CONNECTION_STATES:
                return {
                    "status": normalized_status,
                    "ip_address": ip_address.strip() or None,
                    "error_message": None,
                }
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        status = str(payload.get("status") or "").strip().lower()
        if status not in VPN_CONNECTION_STATES:
            continue
        return {
            "status": status,
            "ip_address": str(
                payload.get("ip_address") or payload.get("ip") or ""
            ).strip()
            or None,
            "error_message": str(payload.get("error_message") or "").strip() or None,
        }
    return None


def classify_vpn_failure(
    lines: Iterable[object], *, fallback_category: str = "process_exit"
) -> dict[str, str]:
    """Classify OpenVPN output without returning any untrusted log content."""
    category = (
        fallback_category
        if fallback_category in VPN_FAILURE_MESSAGES
        else "process_exit"
    )
    normalized_lines = []
    for raw_value in lines:
        raw_line = (
            raw_value.decode("utf-8", errors="replace")
            if isinstance(raw_value, bytes)
            else str(raw_value)
        )
        normalized_lines.append(raw_line.lower())
    for line in reversed(normalized_lines):
        if "starting openvpn attempt " in line:
            break
        matched_category = next(
            (
                candidate
                for candidate, markers in _FAILURE_PATTERNS
                if any(marker in line for marker in markers)
            ),
            None,
        )
        if matched_category is not None:
            category = matched_category
            break
    return {"category": category, "message": VPN_FAILURE_MESSAGES[category]}


def find_vpn_route_overlap(bridge_cidr: str, route_cidrs: Iterable[str]) -> str | None:
    """Return the first IPv4 VPN route overlapping the task bridge subnet."""
    try:
        bridge = ipaddress.ip_network(bridge_cidr, strict=False)
    except ValueError:
        return None
    if bridge.version != 4:
        return None
    for raw_route in route_cidrs:
        candidate = str(raw_route).strip().split(maxsplit=1)[0]
        if not candidate or candidate == "default":
            continue
        try:
            route = ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            continue
        if route.version == 4 and bridge.overlaps(route):
            return str(route)
    return None


def _normalize_timestamp(value: str) -> str:
    """Normalize manager and timezone-less OpenVPN timestamps to RFC3339 UTC."""
    raw_value = value.strip()
    if not raw_value:
        return ""
    normalized = raw_value[:-1] + "+00:00" if raw_value.endswith("Z") else raw_value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_vpn_log_lines(lines: Iterable[object]) -> list[dict[str, str]]:
    """Normalize bounded VPN log lines for the existing terminal log shape."""
    entries: list[dict[str, str]] = []
    for raw_value in lines:
        raw_line = (
            raw_value.decode("utf-8", errors="replace")
            if isinstance(raw_value, bytes)
            else str(raw_value)
        )
        line = raw_line.strip()
        if not line:
            continue
        timestamp = ""
        message = line
        if line.startswith("[") and "] " in line:
            timestamp, message = line[1:].split("] ", 1)
        elif len(line) > 20 and line[:4].isdigit() and line[10:11] == " ":
            timestamp = line[:19]
            message = line[20:]
        timestamp = _normalize_timestamp(timestamp)
        safe_message = _SENSITIVE_PATTERN.sub(r"\1\2<REDACTED>", message)
        normalized = safe_message.lower()
        level = "info"
        if any(marker in normalized for marker in _ERROR_MARKERS):
            level = "error"
        elif any(marker in normalized for marker in _WARNING_MARKERS):
            level = "warning"
        entries.append(
            {
                "timestamp": timestamp,
                "service": "vpn",
                "level": level,
                "message": safe_message,
            }
        )
    return entries


def _read_failure_lines(log_file: str | None) -> list[str]:
    if log_file:
        try:
            return (
                Path(log_file)
                .read_text(encoding="utf-8", errors="replace")
                .splitlines()[-200:]
            )
        except OSError:
            return []
    return sys.stdin.read().splitlines()[-200:]


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify and normalize runtime VPN diagnostics."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    classify = subparsers.add_parser("classify")
    classify.add_argument("--log-file")
    classify.add_argument(
        "--fallback", choices=sorted(VPN_FAILURE_MESSAGES), default="process_exit"
    )
    overlap = subparsers.add_parser("route-overlap")
    overlap.add_argument("--bridge-cidr", required=True)
    args = parser.parse_args(argv)

    if args.command == "classify":
        result = classify_vpn_failure(
            _read_failure_lines(args.log_file), fallback_category=args.fallback
        )
        print(result["category"])
        print(result["message"])
        return 0
    route = find_vpn_route_overlap(args.bridge_cidr, sys.stdin.read().splitlines())
    if route:
        print(route)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
