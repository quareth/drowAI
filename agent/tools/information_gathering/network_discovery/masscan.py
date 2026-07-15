"""Masscan high-speed port scanner tool with runtime-adaptive flag compatibility.

This module supports two schema modes:
- Legacy mode (MASSCAN_SCHEMA_V2=false): preserves historical arguments/behavior.
- V2 mode (default): strict, man-page aligned schema with compatibility aliases.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from ipaddress import ip_address, ip_network
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator

from ...base_tool import BaseTool
from ...canonical_capture import CaptureFamily, CanonicalCaptureFormat, ToolCaptureContract
from ...schemas import BaseToolArgs, ToolResult


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _schema_v2_enabled() -> bool:
    # Default ON so improvements take effect immediately while retaining a rollback switch.
    return _env_bool("MASSCAN_SCHEMA_V2", default=True)


def _safe_inc_metric(name: str, value: int = 1) -> None:
    try:
        from backend.services.metrics.utils import safe_inc

        safe_inc(name, value)
    except Exception:
        pass


@dataclass(frozen=True)
class MasscanCapabilities:
    """Runtime capability snapshot from `masscan --help`."""

    flags: frozenset[str]
    detection_error: Optional[str] = None

    @property
    def available(self) -> bool:
        return bool(self.flags)


@lru_cache(maxsize=1)
def detect_masscan_capabilities() -> MasscanCapabilities:
    """Detect supported masscan flags once per process.

    Falls back to empty capability set if `masscan --help` is unavailable.
    """

    try:
        proc = subprocess.run(
            ["masscan", "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return MasscanCapabilities(flags=frozenset(), detection_error=str(exc))

    help_text = f"{proc.stdout}\n{proc.stderr}"
    if not help_text.strip():
        return MasscanCapabilities(flags=frozenset(), detection_error="empty help output")

    long_flags = set(re.findall(r"--[a-z0-9][a-z0-9-]*", help_text.lower()))
    short_flags = set(re.findall(r"(?<!\S)-[A-Za-z][A-Za-z0-9]?", help_text))

    return MasscanCapabilities(flags=frozenset(long_flags | short_flags), detection_error=None)


def _flag_supported(capabilities: MasscanCapabilities, flag: str) -> bool:
    if not capabilities.available:
        return False
    normalized = {item.lower() for item in capabilities.flags}
    return flag.lower() in normalized


def _choose_flag(
    capabilities: MasscanCapabilities,
    semantic_name: str,
    candidates: List[str],
) -> str:
    """Choose best runtime-supported flag for a semantic option.

    If capabilities are unavailable, fallback to first candidate.
    If capabilities are available and none match, fail fast.
    """

    if not capabilities.available:
        return candidates[0]

    for candidate in candidates:
        if _flag_supported(capabilities, candidate):
            return candidate

    supported_sorted = sorted(capabilities.flags)
    raise ValueError(
        f"Masscan runtime does not support '{semantic_name}'. Tried {candidates}, "
        f"available flags include: {supported_sorted[:40]}"
    )


class RateLimit(str, Enum):
    """Legacy rate presets kept for backwards compatibility."""

    SLOW = "1000"
    NORMAL = "10000"
    FAST = "100000"
    VERY_FAST = "1000000"



class HostDiscoveryMode(str, Enum):
    """Host discovery behavior for masscan."""

    DEFAULT = "default"
    PING_ONLY = "ping_only"
    NO_PING = "no_ping"


def _normalize_and_validate_target_token(token: str) -> str:
    """Validate one target token and return normalized canonical form.

    Supports strict masscan-native IP/CIDR/range targets, plus compatibility
    host/url tokens used by generic schema validators.
    """

    raw_token = str(token or "").strip()
    if not raw_token:
        raise ValueError("target token cannot be empty")

    # Compatibility: allow URL-style targets by extracting hostname.
    if "://" in raw_token:
        parsed = urlsplit(raw_token)
        if parsed.hostname:
            raw_token = str(parsed.hostname).strip()
        else:
            raise ValueError(f"invalid target '{token}': URL target has no hostname")

    if "-" in raw_token:
        start_raw, end_raw = raw_token.split("-", 1)
        if not start_raw or not end_raw:
            raise ValueError(f"invalid IP range target '{raw_token}'")

        try:
            start_ip = ip_address(start_raw)
            end_ip = ip_address(end_raw)
        except ValueError as exc:
            raise ValueError(
                f"invalid target '{raw_token}': targets must be IP/CIDR/range or safe hostname tokens"
            ) from exc

        if start_ip.version != end_ip.version:
            raise ValueError(f"invalid IP range '{raw_token}': mixed address families are not allowed")
        if int(start_ip) > int(end_ip):
            raise ValueError(f"invalid IP range '{raw_token}': start address must be <= end address")

        return f"{start_ip}-{end_ip}"

    if "/" in raw_token:
        try:
            return str(ip_network(raw_token, strict=False))
        except ValueError as exc:
            raise ValueError(
                f"invalid target '{raw_token}': targets must be IP/CIDR/range or safe hostname tokens"
            ) from exc

    try:
        return str(ip_address(raw_token))
    except ValueError:
        # Compatibility fallback for schema contracts and domain-like targets.
        # Keep this strict to avoid shell metacharacters.
        if re.fullmatch(r"[A-Za-z0-9._:-]+", raw_token):
            return raw_token.lower()
        raise ValueError(
            f"invalid target '{raw_token}': targets must be IP/CIDR/range or safe hostname tokens"
        )


def _validate_masscan_target(target: str) -> str:
    """Validate masscan target string.

    Accepted forms:
    - Single IP (IPv4/IPv6)
    - CIDR block (e.g., 192.168.1.0/24)
    - IP range (e.g., 192.168.1.10-192.168.1.100)
    - Multiple targets separated by comma or whitespace
    """

    cleaned = (target or "").strip()
    if not cleaned:
        raise ValueError("target cannot be empty")

    raw_tokens = [part.strip() for part in re.split(r"[,\s]+", cleaned) if part.strip()]
    if not raw_tokens:
        raise ValueError("target must include at least one IP/CIDR/range token")

    normalized_tokens = [_normalize_and_validate_target_token(token) for token in raw_tokens]
    return ",".join(normalized_tokens)


class LegacyMasscanArgs(BaseToolArgs):
    """Legacy masscan argument schema (v1 behavior)."""

    target: str = Field(
        ...,
        description=(
            "Target IP/CIDR/range only (examples: '192.168.1.1', '10.0.0.0/24', "
            "'10.0.0.1-10.0.0.100', '10.0.0.1,10.0.1.0/24'). "
            "Compatibility hostname/url tokens are accepted and normalized safely."
        ),
    )
    ports: Optional[str] = Field(
        None,
        description="Port specification (e.g., '80,443', '1-1000')",
    )
    rate: RateLimit = Field(
        RateLimit.NORMAL,
        description="Rate of packets per second to send",
    )
    max_retries: int = Field(
        3,
        ge=1,
        le=10,
        description="Maximum number of retries for failed packets",
    )
    wait: int = Field(
        10,
        ge=0,
        le=3600,
        description="Seconds to wait after sending last packet",
    )
    interface: Optional[str] = Field(
        None,
        description="Network interface to use for scanning",
    )
    source_ip: Optional[str] = Field(
        None,
        description="Source IP address to use for scanning",
    )
    exclude_file: Optional[str] = Field(
        None,
        description="File containing IP ranges to exclude",
    )
    include_file: Optional[str] = Field(
        None,
        description="File containing IP ranges to include",
    )
    banner: bool = Field(
        False,
        description="Grab banners from open ports",
    )
    ping: bool = Field(
        True,
        description="Enable host-discovery probes during masscan runs",
    )

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return _validate_masscan_target(value)


class MasscanArgsV2(BaseToolArgs):
    """Strict masscan schema aligned with masscan(8) semantics."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(
        ...,
        description=(
            "Target IP/CIDR/range only (examples: '192.168.1.1', '10.0.0.0/24', "
            "'10.0.0.1-10.0.0.100', '10.0.0.1,10.0.1.0/24'). "
            "Compatibility hostname/url tokens are accepted and normalized safely."
        ),
    )
    ports: Optional[str] = Field(
        None,
        description=(
            "Port specification (examples: '80,443', '1-1024', 'U:53,T:22-25'). "
            "Do not use nmap-style 'top-ports'."
        ),
    )
    rate: Optional[int] = Field(
        None,
        ge=1,
        le=10_000_000,
        description="Packet send rate in packets per second.",
    )
    max_rate: Optional[int] = Field(
        None,
        ge=1,
        le=10_000_000,
        description="Maximum transmit rate in packets per second.",
    )
    retries: Optional[int] = Field(
        None,
        ge=0,
        le=100,
        description="Retry count for missed responses.",
    )
    wait: Optional[int] = Field(
        None,
        ge=0,
        le=3600,
        description="Seconds to wait after sending the last packet.",
    )
    adapter: Optional[str] = Field(
        None,
        description="Network adapter/interface name (masscan -e/--adapter).",
    )
    adapter_ip: Optional[str] = Field(
        None,
        description="Source adapter IP address (masscan --adapter-ip).",
    )
    include_file: Optional[str] = Field(
        None,
        description="Path to include-targets file (masscan -iL/--includefile).",
    )
    exclude: Optional[str] = Field(
        None,
        description="Comma-separated targets/ranges to exclude (masscan --exclude).",
    )
    exclude_file: Optional[str] = Field(
        None,
        description="Path to exclude-targets file (masscan --excludefile).",
    )
    open_only: bool = Field(
        False,
        description="Show only hosts with at least one open port.",
    )
    banners: bool = Field(
        False,
        description="Enable banner grabbing (--banners).",
    )
    host_discovery: HostDiscoveryMode = Field(
        HostDiscoveryMode.DEFAULT,
        description="Host discovery mode: default, ping_only, or no_ping.",
    )

    # Deprecated aliases (accepted temporarily for migration)
    max_retries: Optional[int] = Field(
        None,
        ge=0,
        le=100,
        description="DEPRECATED: use 'retries' instead.",
        json_schema_extra={"deprecated": True},
    )
    interface: Optional[str] = Field(
        None,
        description="DEPRECATED: use 'adapter' instead.",
        json_schema_extra={"deprecated": True},
    )
    source_ip: Optional[str] = Field(
        None,
        description="DEPRECATED: use 'adapter_ip' instead.",
        json_schema_extra={"deprecated": True},
    )
    banner: Optional[bool] = Field(
        None,
        description="DEPRECATED: use 'banners' instead.",
        json_schema_extra={"deprecated": True},
    )
    ping: Optional[bool] = Field(
        None,
        description="DEPRECATED: use 'host_discovery' instead.",
        json_schema_extra={"deprecated": True},
    )

    _deprecations: List[str] = PrivateAttr(default_factory=list)

    @field_validator("target")
    @classmethod
    def _validate_target(cls, value: str) -> str:
        return _validate_masscan_target(value)

    @field_validator("ports")
    @classmethod
    def _validate_ports(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("ports cannot be empty")

        if "top-ports" in cleaned.lower():
            raise ValueError("'top-ports' is not supported by masscan; use explicit port ranges")

        tokens = [part.strip() for part in cleaned.split(",") if part.strip()]
        if not tokens:
            raise ValueError("ports must include at least one port token")

        token_re = re.compile(r"^(?:(?:[TU]):)?(\d{1,5})(?:-(\d{1,5}))?$", re.IGNORECASE)
        for token in tokens:
            match = token_re.match(token)
            if not match:
                raise ValueError(
                    "invalid ports token '"
                    + token
                    + "' (allowed examples: 80, 1-1024, U:53, T:22-25)"
                )

            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else start
            if start < 1 or start > 65535 or end < 1 or end > 65535 or start > end:
                raise ValueError(f"invalid port range '{token}'")

        return cleaned

    @model_validator(mode="after")
    def _apply_aliases(self) -> "MasscanArgsV2":
        self._deprecations = []
        fields_set = set(self.model_fields_set)

        self._merge_alias(
            alias_field="max_retries",
            canonical_field="retries",
            fields_set=fields_set,
            deprecation_message="'max_retries' is deprecated; use 'retries'",
        )
        self._merge_alias(
            alias_field="interface",
            canonical_field="adapter",
            fields_set=fields_set,
            deprecation_message="'interface' is deprecated; use 'adapter'",
        )
        self._merge_alias(
            alias_field="source_ip",
            canonical_field="adapter_ip",
            fields_set=fields_set,
            deprecation_message="'source_ip' is deprecated; use 'adapter_ip'",
        )
        self._merge_alias(
            alias_field="banner",
            canonical_field="banners",
            fields_set=fields_set,
            deprecation_message="'banner' is deprecated; use 'banners'",
        )

        if "ping" in fields_set:
            mapped = HostDiscoveryMode.DEFAULT if bool(self.ping) else HostDiscoveryMode.NO_PING
            if "host_discovery" in fields_set and self.host_discovery != mapped:
                raise ValueError(
                    "conflicting parameters: 'ping' and 'host_discovery' do not agree"
                )
            if "host_discovery" not in fields_set:
                self.host_discovery = mapped
            self._deprecations.append("'ping' is deprecated; use 'host_discovery'")

        return self

    def _merge_alias(
        self,
        *,
        alias_field: str,
        canonical_field: str,
        fields_set: set[str],
        deprecation_message: str,
    ) -> None:
        if alias_field not in fields_set:
            return

        alias_value = getattr(self, alias_field)
        canonical_explicit = canonical_field in fields_set
        canonical_value = getattr(self, canonical_field)

        if canonical_explicit and canonical_value != alias_value:
            raise ValueError(
                f"conflicting parameters: '{alias_field}'={alias_value!r} and "
                f"'{canonical_field}'={canonical_value!r}"
            )

        if not canonical_explicit:
            setattr(self, canonical_field, alias_value)

        self._deprecations.append(deprecation_message)

    @property
    def deprecations(self) -> List[str]:
        return list(self._deprecations)


MasscanArgs = MasscanArgsV2 if _schema_v2_enabled() else LegacyMasscanArgs


def parse_masscan_json(json_text: str) -> Dict[str, Any]:
    """Parse masscan JSON output into structured metadata.

    Supports both array-style JSON and line-delimited JSON objects, while
    ignoring progress lines that may be mixed into output.
    """

    metadata: Dict[str, Any] = {"open_ports": [], "hosts": []}
    objects: List[Dict[str, Any]] = []

    text = (json_text or "").strip()
    if not text:
        return metadata

    if text.startswith("["):
        try:
            payload = json.loads(text)
            if isinstance(payload, list):
                objects.extend(item for item in payload if isinstance(item, dict))
        except json.JSONDecodeError:
            pass

    if not objects:
        for raw_line in text.splitlines():
            line = raw_line.strip().rstrip(",")
            if not line or line in {"[", "]"} or not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                objects.append(data)

    if not objects:
        metadata["error"] = "Failed to parse masscan JSON output"
        return metadata

    for data in objects:
        ports = data.get("ports")
        if not isinstance(ports, list):
            continue

        for port_info in ports:
            if not isinstance(port_info, dict):
                continue
            metadata["open_ports"].append(
                {
                    "port": port_info.get("port"),
                    "protocol": port_info.get("proto"),
                    "status": port_info.get("status"),
                    "service": port_info.get("service", "unknown"),
                }
            )

        metadata["hosts"].append(
            {
                "ip": data.get("ip"),
                "timestamp": data.get("timestamp"),
                "ports_count": len(ports),
            }
        )

    return metadata


class MasscanTool(BaseTool):
    """Run masscan scans and parse the results.

    Supports PTY execution via build_command(), parse_output(), and create_artifacts().
    """

    args_model = MasscanArgs
    _capture_contract = ToolCaptureContract(
        family=CaptureFamily.STRUCTURED_NATIVE,
        canonical_format=CanonicalCaptureFormat.JSON,
    )

    def _build_legacy_command(self, args: LegacyMasscanArgs) -> List[str]:
        """Legacy command builder retained for feature-flag rollback."""

        cmd = ["masscan"]
        cmd.extend(["--rate", args.rate.value])

        if args.ports:
            cmd.extend(["-p", args.ports])

        # Canonical internal capture: always JSON for structured metadata extraction
        cmd.extend(["-oJ", "-"])

        cmd.extend(["--max-retries", str(args.max_retries)])
        cmd.extend(["--wait", str(args.wait)])

        if args.interface:
            cmd.extend(["-i", args.interface])

        if args.source_ip:
            cmd.extend(["--source-ip", args.source_ip])

        if args.exclude_file:
            cmd.extend(["--exclude-file", args.exclude_file])

        if args.include_file:
            cmd.extend(["--include-file", args.include_file])

        if args.banner:
            cmd.append("--banners")

        if not args.ping:
            cmd.append("--no-ping")

        cmd.append(args.target)
        return cmd

    def _build_v2_command(self, args: MasscanArgsV2) -> List[str]:
        """Build v2 masscan command using runtime-compatible flag aliases."""

        cmd = ["masscan"]
        capabilities = detect_masscan_capabilities()

        if args.rate is not None:
            cmd.extend(["--rate", str(args.rate)])

        if args.max_rate is not None:
            cmd.extend(["--max-rate", str(args.max_rate)])

        if args.ports:
            cmd.extend(["-p", args.ports])

        # Canonical internal capture: always JSON for structured metadata extraction
        cmd.extend(["-oJ", "-"])

        if args.retries is not None:
            retries_flag = _choose_flag(
                capabilities,
                "retries",
                ["--retries", "--max-retries"],
            )
            cmd.extend([retries_flag, str(args.retries)])

        if args.wait is not None:
            cmd.extend(["--wait", str(args.wait)])

        if args.adapter:
            adapter_flag = _choose_flag(
                capabilities,
                "adapter",
                ["-e", "--adapter"],
            )
            cmd.extend([adapter_flag, args.adapter])

        if args.adapter_ip:
            adapter_ip_flag = _choose_flag(
                capabilities,
                "adapter_ip",
                ["--adapter-ip", "--source-ip"],
            )
            cmd.extend([adapter_ip_flag, args.adapter_ip])

        if args.include_file:
            include_flag = _choose_flag(
                capabilities,
                "include_file",
                ["-iL", "--includefile", "--include-file"],
            )
            cmd.extend([include_flag, args.include_file])

        if args.exclude:
            cmd.extend(["--exclude", args.exclude])

        if args.exclude_file:
            exclude_file_flag = _choose_flag(
                capabilities,
                "exclude_file",
                ["--excludefile", "--exclude-file"],
            )
            cmd.extend([exclude_file_flag, args.exclude_file])

        if args.open_only:
            cmd.append("--open-only")

        if args.banners:
            cmd.append("--banners")

        if args.host_discovery == HostDiscoveryMode.PING_ONLY:
            if capabilities.available and not _flag_supported(capabilities, "--ping"):
                raise ValueError("Masscan runtime does not support host_discovery='ping_only' (--ping)")
            cmd.append("--ping")
        elif args.host_discovery == HostDiscoveryMode.NO_PING:
            if capabilities.available and not _flag_supported(capabilities, "--no-ping"):
                raise ValueError("Masscan runtime does not support host_discovery='no_ping' (--no-ping)")
            cmd.append("--no-ping")

        cmd.append(args.target)
        return cmd

    def build_command(self, args: MasscanArgs) -> List[str]:
        """Build masscan command arguments from validated parameters."""

        if not _schema_v2_enabled() and isinstance(args, LegacyMasscanArgs):
            return self._build_legacy_command(args)
        return self._build_v2_command(args)  # type: ignore[arg-type]

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: MasscanArgs,
    ) -> Dict[str, Any]:
        """Parse masscan JSON output into structured metadata."""

        metadata: Dict[str, Any] = {}
        if stdout:
            metadata = parse_masscan_json(stdout)

        if _schema_v2_enabled() and hasattr(args, "deprecations"):
            deprecations = getattr(args, "deprecations", [])
            if deprecations:
                metadata["deprecations"] = list(deprecations)

        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: MasscanArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create masscan JSON artifact files from output."""

        artifacts: List[str] = []

        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/masscan_{ts}.json"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass

        return artifacts

    def run(self, args: MasscanArgs) -> ToolResult:
        """Execute masscan scan with structured failure handling."""

        start = time.time()

        try:
            cmd = self.build_command(args)
        except (ValidationError, ValueError) as exc:
            _safe_inc_metric("masscan_validation_error_total")
            reason = str(exc).split(".")[0][:80].strip().replace(" ", "_").replace("-", "_")
            if reason:
                _safe_inc_metric(f"masscan_validation_error_total_{reason}")
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=f"Validation error: {exc}",
                artifacts=[],
                metadata={"error_type": "validation_error"},
                execution_time=time.time() - start,
            )

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout="",
                stderr="Command timed out",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )

        if proc.returncode != 0:
            _safe_inc_metric("masscan_non_timeout_failure_total")
            stderr_lower = (proc.stderr or "").lower()
            if "unknown option" in stderr_lower or "unrecognized option" in stderr_lower:
                _safe_inc_metric("masscan_cli_unknown_option_total")

        metadata = self.parse_output(proc.stdout, proc.stderr, proc.returncode, args)
        artifacts = self.create_artifacts(proc.stdout, args, timestamp=int(start))

        return ToolResult(
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start,
        )


# ---------------------------------------------------------------------------
# Tool Metadata Registration
# ---------------------------------------------------------------------------
from ...enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="information_gathering.network_discovery.masscan",
        display_name="Masscan",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="fast_port_discovery",
                description="Discover open ports at high speed across very large IP ranges; returns open ports; prefer for large/full-port sweeps where speed matters, not for service or OS detection",
                output_indicators=["open"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp", "udp"],
        execution_priority=8,
        parallel_compatible=True,
        stealth_level=2,
        estimated_runtime_minutes=3,
        best_combined_with=["information_gathering.network_discovery.nmap"],
    )
)
