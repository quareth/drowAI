"""Finite network utility tool and planner metadata registration.

This module exposes a grouped, non-interactive utility surface for bounded
network diagnostics such as ping, DNS lookup, WHOIS, TCP connect checks, and
local routing/interface inspection. It intentionally does not perform scanning,
HTTP fetching, packet capture, or shell execution.
"""

from __future__ import annotations

import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..base_tool import BaseTool
from ..categories import PentestPhase, ToolCategory
from ..enhanced_metadata import EnhancedToolMetadata, ToolCapability, ToolCatalogRole
from ..enhanced_metadata_registry import register_enhanced_tool_metadata
from ..schemas import ToolResult


_REMOTE_OPERATIONS = {
    "ping",
    "dns_lookup",
    "whois",
    "tcp_connect",
    "trace_route",
}
_LOCAL_OPERATIONS = {"local_interfaces", "local_routes", "local_neighbors"}
_MAX_PREVIEW_CHARS = 4000


def _strip_json_schema_defaults(schema: Dict[str, Any], *_args: Any) -> None:
    """Remove defaults and add operation-specific finite requirements."""
    if not isinstance(schema, dict):
        return

    def strip_defaults(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            for value in node.values():
                strip_defaults(value)
        elif isinstance(node, list):
            for value in node:
                strip_defaults(value)

    strip_defaults(schema)
    schema.setdefault("allOf", []).extend(
        [
            {
                "if": {
                    "properties": {"operation": {"const": "ping"}},
                    "required": ["operation"],
                },
                "then": {
                    "required": [
                        "operation",
                        "target",
                        "count",
                        "timeout_sec",
                        "per_probe_timeout_sec",
                    ]
                },
            },
            {
                "if": {
                    "properties": {"operation": {"const": "dns_lookup"}},
                    "required": ["operation"],
                },
                "then": {
                    "required": [
                        "operation",
                        "target",
                        "record_type",
                        "timeout_sec",
                        "per_probe_timeout_sec",
                    ]
                },
            },
            {
                "if": {
                    "properties": {"operation": {"const": "whois"}},
                    "required": ["operation"],
                },
                "then": {"required": ["operation", "target", "timeout_sec"]},
            },
            {
                "if": {
                    "properties": {"operation": {"const": "tcp_connect"}},
                    "required": ["operation"],
                },
                "then": {
                    "required": [
                        "operation",
                        "target",
                        "port",
                        "timeout_sec",
                        "per_probe_timeout_sec",
                    ]
                },
            },
            {
                "if": {
                    "properties": {"operation": {"const": "trace_route"}},
                    "required": ["operation"],
                },
                "then": {
                    "required": [
                        "operation",
                        "target",
                        "timeout_sec",
                        "per_probe_timeout_sec",
                        "max_hops",
                        "queries",
                    ]
                },
            },
            {
                "if": {
                    "properties": {"operation": {"const": "local_interfaces"}},
                    "required": ["operation"],
                },
                "then": {"required": ["operation", "timeout_sec"]},
            },
            {
                "if": {
                    "properties": {"operation": {"const": "local_routes"}},
                    "required": ["operation"],
                },
                "then": {"required": ["operation", "timeout_sec"]},
            },
            {
                "if": {
                    "properties": {"operation": {"const": "local_neighbors"}},
                    "required": ["operation"],
                },
                "then": {"required": ["operation", "timeout_sec"]},
            },
        ]
    )


class NetworkUtilityOperation(str, Enum):
    """Supported finite network utility operations."""

    PING = "ping"
    DNS_LOOKUP = "dns_lookup"
    WHOIS = "whois"
    TCP_CONNECT = "tcp_connect"
    TRACE_ROUTE = "trace_route"
    LOCAL_INTERFACES = "local_interfaces"
    LOCAL_ROUTES = "local_routes"
    LOCAL_NEIGHBORS = "local_neighbors"


class DnsRecordType(str, Enum):
    """DNS record types supported by the dig utility operation."""

    A = "A"
    AAAA = "AAAA"
    CNAME = "CNAME"
    MX = "MX"
    NS = "NS"
    TXT = "TXT"
    SOA = "SOA"
    PTR = "PTR"
    SRV = "SRV"
    ANY = "ANY"


class NetworkUtilityArgs(BaseModel):
    """Arguments for finite network utility checks."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra=_strip_json_schema_defaults,
    )

    operation: NetworkUtilityOperation = Field(
        ...,
        description=(
            "Utility operation to run: ping, dns_lookup, whois, tcp_connect, "
            "trace_route, local_interfaces, local_routes, or local_neighbors."
        ),
    )
    target: Optional[str] = Field(
        None,
        min_length=1,
        max_length=255,
        description=(
            "Target hostname, IP, domain, ASN, or reverse-DNS name. Required for "
            "remote operations; omit for local interface/route/neighbor operations."
        ),
    )
    port: Optional[int] = Field(
        None,
        ge=1,
        le=65535,
        description="TCP port for operation=tcp_connect.",
    )
    record_type: Optional[DnsRecordType] = Field(
        None,
        description="DNS record type. Required for operation=dns_lookup.",
    )
    resolver: Optional[str] = Field(
        None,
        min_length=1,
        max_length=255,
        description="Optional DNS resolver hostname or IP for operation=dns_lookup.",
    )
    count: Optional[int] = Field(
        None,
        ge=1,
        le=10,
        description="Finite echo request count. Required for operation=ping.",
    )
    timeout_sec: Optional[int] = Field(
        None,
        ge=1,
        le=60,
        description="Global subprocess timeout in seconds. Required for every operation.",
    )
    per_probe_timeout_sec: Optional[int] = Field(
        None,
        ge=1,
        le=10,
        description=(
            "Per-probe timeout. Required for operation=ping, operation=dns_lookup, "
            "operation=tcp_connect, and operation=trace_route."
        ),
    )
    max_hops: Optional[int] = Field(
        None,
        ge=1,
        le=64,
        description="Maximum traceroute hops. Required for operation=trace_route.",
    )
    queries: Optional[int] = Field(
        None,
        ge=1,
        le=3,
        description="Traceroute probes per hop. Required for operation=trace_route.",
    )

    @field_validator("target", "resolver")
    @classmethod
    def validate_host_token(cls, value: Optional[str]) -> Optional[str]:
        """Reject ambiguous option-like or whitespace-bearing target tokens."""
        if value is None:
            return None
        token = value.strip()
        if not token:
            raise ValueError("value cannot be empty")
        if token.startswith("-"):
            raise ValueError("value cannot start with '-'")
        if any(ch.isspace() for ch in token):
            raise ValueError("value cannot contain whitespace")
        if any(ord(ch) < 32 for ch in token):
            raise ValueError("value cannot contain control characters")
        return token

    @model_validator(mode="after")
    def validate_operation_fields(self) -> "NetworkUtilityArgs":
        operation = self.operation.value

        if operation in _REMOTE_OPERATIONS and not self.target:
            raise ValueError(f"target is required for operation={operation}")
        if operation in _LOCAL_OPERATIONS and self.target:
            raise ValueError(f"target must be omitted for operation={operation}")

        if self.timeout_sec is None:
            raise ValueError(f"timeout_sec is required for operation={operation}")

        if operation == NetworkUtilityOperation.PING.value:
            if self.count is None:
                raise ValueError("count is required for operation=ping")
            if self.per_probe_timeout_sec is None:
                raise ValueError("per_probe_timeout_sec is required for operation=ping")
        elif self.count is not None:
            raise ValueError("count is only valid for operation=ping")

        if operation == NetworkUtilityOperation.TCP_CONNECT.value:
            if self.port is None:
                raise ValueError("port is required for operation=tcp_connect")
            if self.per_probe_timeout_sec is None:
                raise ValueError("per_probe_timeout_sec is required for operation=tcp_connect")
        elif self.port is not None:
            raise ValueError("port is only valid for operation=tcp_connect")

        if operation == NetworkUtilityOperation.DNS_LOOKUP.value:
            if self.record_type is None:
                raise ValueError("record_type is required for operation=dns_lookup")
            if self.per_probe_timeout_sec is None:
                raise ValueError("per_probe_timeout_sec is required for operation=dns_lookup")
        else:
            if self.resolver is not None:
                raise ValueError("resolver is only valid for operation=dns_lookup")
            if self.record_type is not None:
                raise ValueError("record_type is only valid for operation=dns_lookup")

        if operation == NetworkUtilityOperation.TRACE_ROUTE.value:
            if self.max_hops is None:
                raise ValueError("max_hops is required for operation=trace_route")
            if self.queries is None:
                raise ValueError("queries is required for operation=trace_route")
            if self.per_probe_timeout_sec is None:
                raise ValueError("per_probe_timeout_sec is required for operation=trace_route")
        else:
            if self.max_hops is not None:
                raise ValueError("max_hops is only valid for operation=trace_route")
            if self.queries is not None:
                raise ValueError("queries is only valid for operation=trace_route")

        if operation not in {
            NetworkUtilityOperation.PING.value,
            NetworkUtilityOperation.DNS_LOOKUP.value,
            NetworkUtilityOperation.TCP_CONNECT.value,
            NetworkUtilityOperation.TRACE_ROUTE.value,
        } and self.per_probe_timeout_sec is not None:
            raise ValueError(
                "per_probe_timeout_sec is only valid for operation=ping, "
                "operation=dns_lookup, operation=tcp_connect, or operation=trace_route"
            )

        return self


class NetworkUtilityTool(BaseTool):
    """Run finite network utility diagnostics without shell access."""

    tool_id = "networking_utilities.network"
    args_model = NetworkUtilityArgs

    def build_command(self, args: NetworkUtilityArgs) -> List[str]:
        operation = args.operation
        target = args.target or ""

        if operation is NetworkUtilityOperation.PING:
            return [
                "ping",
                "-c",
                str(args.count),
                "-W",
                str(args.per_probe_timeout_sec),
                "-w",
                str(args.timeout_sec),
                target,
            ]

        if operation is NetworkUtilityOperation.DNS_LOOKUP:
            record_type = args.record_type.value if args.record_type else ""
            command = ["dig"]
            if args.resolver:
                command.append(f"@{args.resolver}")
            command.extend([
                target,
                record_type,
                f"+time={args.per_probe_timeout_sec}",
                "+tries=1",
            ])
            return command

        if operation is NetworkUtilityOperation.WHOIS:
            return ["whois", target]

        if operation is NetworkUtilityOperation.TCP_CONNECT:
            return [
                "nc",
                "-vz",
                "-w",
                str(args.per_probe_timeout_sec),
                target,
                str(args.port),
            ]

        if operation is NetworkUtilityOperation.TRACE_ROUTE:
            return [
                "traceroute",
                "-m",
                str(args.max_hops),
                "-q",
                str(args.queries),
                "-w",
                str(args.per_probe_timeout_sec),
                target,
            ]

        if operation is NetworkUtilityOperation.LOCAL_INTERFACES:
            return ["ip", "-brief", "addr"]

        if operation is NetworkUtilityOperation.LOCAL_ROUTES:
            return ["ip", "route", "show"]

        if operation is NetworkUtilityOperation.LOCAL_NEIGHBORS:
            return ["ip", "neigh", "show"]

        raise ValueError(f"unsupported network utility operation: {operation}")

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: NetworkUtilityArgs,
    ) -> Dict[str, Any]:
        lines = [line for line in (stdout or "").splitlines() if line.strip()]
        metadata: Dict[str, Any] = {
            "operation": args.operation.value,
            "target": args.target,
            "command": self.build_command(args),
            "exit_code": exit_code,
            "success": exit_code == 0,
            "timed_out": False,
            "stdout_preview": (stdout or "")[:_MAX_PREVIEW_CHARS],
            "stderr_preview": (stderr or "")[:_MAX_PREVIEW_CHARS],
            "stdout_line_count": len(lines),
        }

        if args.operation is NetworkUtilityOperation.DNS_LOOKUP:
            metadata["record_type"] = args.record_type.value if args.record_type else None
            metadata["answer_count"] = len(lines)
        elif args.operation is NetworkUtilityOperation.TCP_CONNECT:
            metadata["port"] = args.port
            metadata["reachable"] = exit_code == 0
        elif args.operation in {
            NetworkUtilityOperation.LOCAL_INTERFACES,
            NetworkUtilityOperation.LOCAL_ROUTES,
            NetworkUtilityOperation.LOCAL_NEIGHBORS,
        }:
            metadata["entry_count"] = len(lines)

        return metadata

    def run(self, args: NetworkUtilityArgs) -> ToolResult:
        start = time.time()
        command = self.build_command(args)
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=args.timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            execution_time = time.time() - start
            stdout = exc.stdout or ""
            stderr = exc.stderr or "Command timed out"
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            metadata = {
                "operation": args.operation.value,
                "target": args.target,
                "command": command,
                "exit_code": -2,
                "success": False,
                "timed_out": True,
                "duration_ms": int(execution_time * 1000),
                "stdout_preview": stdout[:_MAX_PREVIEW_CHARS],
                "stderr_preview": stderr[:_MAX_PREVIEW_CHARS],
            }
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout=stdout,
                stderr=stderr,
                artifacts=[],
                metadata=metadata,
                execution_time=execution_time,
            )
        except FileNotFoundError as exc:
            execution_time = time.time() - start
            stderr = f"Command not found: {command[0]}"
            metadata = {
                "operation": args.operation.value,
                "target": args.target,
                "command": command,
                "exit_code": -127,
                "success": False,
                "timed_out": False,
                "duration_ms": int(execution_time * 1000),
                "stdout_preview": "",
                "stderr_preview": stderr,
                "error": str(exc),
            }
            return ToolResult(
                success=False,
                exit_code=-127,
                stdout="",
                stderr=stderr,
                artifacts=[],
                metadata=metadata,
                execution_time=execution_time,
            )

        execution_time = time.time() - start
        metadata = self.parse_output(
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.returncode,
            args=args,
        )
        metadata["duration_ms"] = int(execution_time * 1000)

        return ToolResult(
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifacts=[],
            metadata=metadata,
            execution_time=execution_time,
        )


register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id=NetworkUtilityTool.tool_id,
        display_name="Network Utility",
        category=ToolCategory.NETWORKING_UTILITIES,
        catalog_role=ToolCatalogRole.UTILITY,
        applicable_phases=[
            PentestPhase.RECONNAISSANCE,
            PentestPhase.ENUMERATION,
        ],
        capabilities=[
            ToolCapability(
                name="finite_network_utility",
                description=(
                    "Run finite network utility checks: ping, dig, whois, TCP connect, "
                    "traceroute, and local ip route/interface/neighbor inspection; "
                    "returns command metadata; not for scanning or HTTP"
                ),
                output_indicators=[
                    "operation",
                    "command",
                    "exit_code",
                    "stdout_preview",
                    "stderr_preview",
                ],
            )
        ],
        required_services=[],
        target_protocols=["icmp", "dns", "whois", "tcp", "local"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=5,
        estimated_runtime_minutes=1,
        supported_transports=["direct", "file-comm", "pty"],
    )
)
