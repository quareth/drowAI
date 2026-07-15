"""Nmap network scanner tool using Pydantic models."""

from __future__ import annotations

import os
import subprocess
import time
import xml.etree.ElementTree as ET
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field, validator
from runtime_shared.semantic.service_identity import build_service_socket_key

from ...base_tool import BaseTool
from ...canonical_capture import CaptureFamily, CanonicalCaptureFormat, ToolCaptureContract
from ...schemas import BaseToolArgs, ToolResult
from .nmap_semantics import (
    build_nmap_semantic_evidence,
    build_host_profiled_observation,
    build_semantic_transport_markers,
    build_service_detected_payload,
    build_service_profiled_observation,
    classify_script_findings,
    enrich_host,
    enrich_port,
)


class ScanType(str, Enum):
    """Supported nmap scan techniques."""

    TCP_SYN = "-sS"
    TCP_CONNECT = "-sT"
    UDP = "-sU"
    SCTP_INIT = "-sY"
    TCP_NULL = "-sN"
    TCP_FIN = "-sF"
    TCP_XMAS = "-sX"
    SERVICE_DETECTION = "-sV"
    HOST_DISCOVERY = "-sn"


class TimingTemplate(str, Enum):
    """Nmap timing templates."""

    PARANOID = "-T0"
    SNEAKY = "-T1"
    POLITE = "-T2"
    NORMAL = "-T3"
    AGGRESSIVE = "-T4"
    INSANE = "-T5"


class NmapArgs(BaseToolArgs):
    """Nmap network scanner arguments.

    Each field maps directly to an nmap CLI flag.
    Only set parameters the user explicitly requested.

    Constraint: scan_types=["-sn"] (host discovery) and the ports field
    are mutually exclusive — nmap errors if both are specified.
    """

    ports: Optional[str] = Field(
        None,
        description='Port specification (-p). E.g. "80,443", "1-1000", "1-65535". '
                    "Omit to use nmap default top ports. "
                    'Must be omitted when scan_types includes "-sn".',
    )
    scan_types: List[ScanType] = Field(
        default_factory=list,
        description='Scan technique(s) — maps to nmap -s* flags. '
                    "Only include types the user explicitly requested. "
                    "When omitted, nmap uses its own default (SYN scan as root).",
    )
    timing: TimingTemplate = Field(
        TimingTemplate.AGGRESSIVE,
        description="Timing template controlling scan speed vs stealth",
    )
    default_scripts: bool = Field(
        False,
        description="Run default NSE scripts (-sC flag). Equivalent to --script=default.",
    )
    scripts: Optional[List[str]] = Field(
        None,
        description="Nmap Scripting Engine (NSE) scripts to run (e.g., ['vuln', 'safe'])",
    )
    service_detection: bool = Field(
        False,
        description="Enable service version detection (-sV flag)",
    )
    os_detection: bool = Field(
        False,
        description="Enable operating system detection (-O flag)",
    )
    aggressive: bool = Field(
        False,
        description="Enable aggressive scan options (-A flag) - combines -O, -sV, -sC, --traceroute",
    )
    skip_host_discovery: bool = Field(
        False,
        description="Skip host discovery and treat all targets as online (-Pn)",
    )
    disable_dns: bool = Field(
        False,
        description="Disable reverse DNS resolution to speed up scans (-n)",
    )
    max_rate: Optional[int] = Field(
        None,
        gt=0,
        description="Maximum number of packets per second (--max-rate)",
    )
    min_rate: Optional[int] = Field(
        None,
        gt=0,
        description="Minimum number of packets per second (--min-rate)",
    )
    scan_delay_ms: Optional[int] = Field(
        None,
        gt=0,
        description="Delay between probes in milliseconds (--scan-delay)",
    )
    script_categories: Optional[List[str]] = Field(
        None,
        description="NSE script categories to execute (e.g., ['default', 'vuln'])",
    )
    script_args: Optional[Dict[str, str]] = Field(
        None,
        description="Arguments passed to NSE scripts (--script-args, key=value)",
    )

    @validator("scan_types", pre=True)
    def _normalize_scan_types(cls, value):  # type: ignore[override]
        if not value:
            return value

        normalized: List[ScanType] = []
        for item in value:
            if isinstance(item, ScanType):
                normalized.append(item)
                continue

            candidate = str(item).strip()
            # Map deprecated "-sP" to HOST_DISCOVERY enum member
            if candidate == "-sP":
                normalized.append(ScanType.HOST_DISCOVERY)
                continue

            # Try to convert string to ScanType enum member
            try:
                # First try direct value lookup
                scan_type = ScanType(candidate)
                normalized.append(scan_type)
            except ValueError:
                # If direct lookup fails, try matching by value
                for scan_type_enum in ScanType:
                    if scan_type_enum.value == candidate:
                        normalized.append(scan_type_enum)
                        break
                else:
                    # If no match found, raise error for invalid scan type
                    raise ValueError(
                        f"Invalid scan type '{candidate}'. "
                        f"Valid options: {[st.value for st in ScanType]}"
                    )

        return normalized


def parse_nmap_xml(xml_text: str) -> Dict[str, Any]:
    """Parse nmap XML output into structured metadata.
    
    Returns metadata including:
    - open_ports: List of discovered open ports
    - hosts_up: Number of hosts that responded
    - hosts_total: Total hosts scanned
    - hosts: List of host details (IP, status, ports)
    """

    metadata: Dict[str, Any] = {
        "open_ports": [],
        "hosts_up": 0,
        "hosts_total": 0,
        "hosts": [],
    }
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        metadata["error"] = "Failed to parse XML"
        return metadata

    # Extract run stats for host counts
    runstats = root.find("runstats")
    if runstats is not None:
        hosts_el = runstats.find("hosts")
        if hosts_el is not None:
            metadata["hosts_up"] = int(hosts_el.attrib.get("up", 0))
            metadata["hosts_total"] = int(hosts_el.attrib.get("total", 0))
            metadata["hosts_down"] = int(hosts_el.attrib.get("down", 0))

    # Parse all hosts (not just first one)
    for host in root.findall("host"):
        host_info: Dict[str, Any] = {"ports": []}

        # Get host address
        addr_el = host.find("address")
        if addr_el is not None:
            host_info["ip"] = addr_el.attrib.get("addr")
            host_info["addr_type"] = addr_el.attrib.get("addrtype")

        # Get host status
        status_el = host.find("status")
        if status_el is not None:
            host_info["status"] = status_el.attrib.get("state")

        # Get ports for this host
        for port_el in host.findall("ports/port"):
            state_el = port_el.find("state")
            if state_el is not None and state_el.attrib.get("state") == "open":
                service_el = port_el.find("service")
                port_info = {
                    "port": int(port_el.attrib.get("portid", 0)),
                    "protocol": port_el.attrib.get("protocol"),
                    "service": service_el.attrib.get("name") if service_el is not None else None,
                    "product": service_el.attrib.get("product") if service_el is not None else None,
                    "version": service_el.attrib.get("version") if service_el is not None else None,
                }
                # Enrich port with service profile (scripts, http_title, etc.)
                enrich_port(port_el, port_info)
                host_info["ports"].append(port_info)
                # Also add to flat open_ports list for backward compatibility
                metadata["open_ports"].append(port_info)

        # Enrich host with rich metadata (hostnames, OS, scripts, traceroute)
        enrich_host(host, host_info)

        metadata["hosts"].append(host_info)
    
    # Legacy single-host support: set host_status from first host
    if metadata["hosts"]:
        metadata["host_status"] = metadata["hosts"][0].get("status")
    
    return metadata


class NmapTool(BaseTool):
    """Run nmap scans and parse the results.

    Supports PTY execution via build_command(), parse_output(), and create_artifacts().
    """

    args_model = NmapArgs
    _capture_contract = ToolCaptureContract(
        family=CaptureFamily.STRUCTURED_NATIVE,
        canonical_format=CanonicalCaptureFormat.XML,
    )

    def build_command(self, args: NmapArgs) -> List[str]:
        """Build nmap command arguments.
        
        This method is used by both run() and PTY execution,
        ensuring consistent command construction.
        
        Args:
            args: Validated NmapArgs
            
        Returns:
            List of command arguments for nmap
        """
        cmd = ["nmap"]
        cmd.append(args.timing.value)
        cmd.extend(t.value for t in args.scan_types)
        
        if args.skip_host_discovery:
            cmd.append("-Pn")
        if args.disable_dns:
            cmd.append("-n")
        if args.ports:
            cmd.extend(["-p", args.ports])
        if args.service_detection:
            cmd.append("-sV")
        if args.os_detection:
            cmd.append("-O")
        if args.aggressive:
            cmd.append("-A")
        if args.default_scripts:
            cmd.append("-sC")
        if args.max_rate is not None:
            cmd.extend(["--max-rate", str(args.max_rate)])
        if args.min_rate is not None:
            cmd.extend(["--min-rate", str(args.min_rate)])
        if args.scan_delay_ms is not None:
            cmd.extend(["--scan-delay", f"{args.scan_delay_ms}ms"])

        scripts_to_run: List[str] = []
        if args.scripts:
            scripts_to_run.extend(args.scripts)
        if args.script_categories:
            scripts_to_run.extend(args.script_categories)
        if scripts_to_run:
            cmd.extend(["--script", ",".join(scripts_to_run)])
        if args.script_args:
            serialized_args = ",".join(
                f"{key}={value}" for key, value in args.script_args.items()
            )
            if serialized_args:
                cmd.extend(["--script-args", serialized_args])
        
        # Canonical internal capture: always XML for structured metadata extraction
        cmd.extend(["-oX", "-"])
        
        # Handle multiple targets: split comma/space-separated targets into individual args
        # Nmap expects each target as a separate command-line argument
        target_string = args.target.strip()
        if "," in target_string or " " in target_string:
            # Split by comma or whitespace, filter empty strings
            targets = [t.strip() for t in target_string.replace(",", " ").split() if t.strip()]
            cmd.extend(targets)
        else:
            cmd.append(target_string)
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: NmapArgs,
    ) -> Dict[str, Any]:
        """Parse nmap XML output into structured metadata.

        Args:
            stdout: Command stdout (always XML via canonical capture)
            stderr: Command stderr
            exit_code: Command exit code
            args: Original NmapArgs

        Returns:
            Metadata dict with open_ports, hosts_up, hosts_total, etc.
        """
        if stdout:
            metadata = parse_nmap_xml(stdout)
            metadata.update(build_semantic_transport_markers())
            return metadata
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: NmapArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create nmap XML artifact files from output.

        Args:
            stdout: Command stdout (always XML via canonical capture)
            args: Original NmapArgs
            timestamp: Optional timestamp for artifact naming

        Returns:
            List of artifact file paths created
        """
        artifacts: List[str] = []

        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/nmap_{ts}.xml"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass  # Artifact creation is optional

        return artifacts

    def emit_semantic_observations(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: NmapArgs,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Emit canonical semantic observations from parsed nmap metadata.

        Produces existing inventory observations (host_discovered, open_port,
        service_detected) plus new profiling observations (host_profiled,
        service_profiled) and curated findings from the allowlist.

        All observations are built from already-normalized metadata produced
        by parse_output(), not from re-parsing XML.
        """
        observations: List[Dict[str, Any]] = []
        hosts = metadata.get("hosts", [])
        if not hosts:
            return observations

        for host_info in hosts:
            ip = str(host_info.get("ip") or "").strip()
            if not ip:
                continue

            # --- Existing: host discovered ---
            observations.append({
                "observation_type": "network.host_discovered",
                "subject_type": "host.ip",
                "subject_key": f"host.ip:{ip}",
                "payload": {"source": "nmap"},
            })

            # --- New: host profiled (rich) ---
            host_obs = build_host_profiled_observation(ip, host_info)
            if host_obs is not None:
                observations.append(host_obs)

            for port_info in host_info.get("ports", []):
                port = port_info.get("port")
                protocol = port_info.get("protocol", "tcp")
                service_name = str(port_info.get("service") or "").strip()
                try:
                    service_key = build_service_socket_key(ip=ip, protocol=protocol, port=port)
                except ValueError:
                    continue

                # --- Existing: open port ---
                observations.append({
                    "observation_type": "network.open_port",
                    "subject_type": "service.socket",
                    "subject_key": service_key,
                    "payload": {
                        "ip": ip,
                        "protocol": protocol,
                        "port": port,
                        "source": "nmap",
                    },
                })

                # --- Existing: service detected ---
                if service_name and service_name.lower() not in {"unknown", "?"}:
                    observations.append({
                        "observation_type": "network.service_detected",
                        "subject_type": "service.socket",
                        "subject_key": service_key,
                        "payload": build_service_detected_payload(port_info),
                    })

                # --- New: service profiled (rich) ---
                svc_obs = build_service_profiled_observation(ip, port_info)
                if svc_obs is not None:
                    observations.append(svc_obs)

                # --- New: curated findings from allowlist ---
                profile = port_info.get("service_profile")
                if profile and profile.get("script_summaries"):
                    findings = classify_script_findings(
                        ip, port, protocol, profile["script_summaries"],
                    )
                    observations.extend(findings)

        return observations

    def emit_semantic_evidence(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: NmapArgs,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Delegate nmap semantic evidence emission to the shared semantics builder."""
        _ = stdout, stderr, exit_code
        return build_nmap_semantic_evidence(metadata, args)

    def run(self, args: NmapArgs) -> ToolResult:
        """Execute nmap scan.
        
        Uses build_command(), parse_output(), and create_artifacts() for
        consistent behavior with PTY execution path.
        """
        cmd = self.build_command(args)  # Reuse build_command!
        
        start = time.time()
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

        # Reuse parse_output and create_artifacts!
        metadata = self.parse_output(proc.stdout, proc.stderr, proc.returncode, args)
        artifacts = self.create_artifacts(proc.stdout, args, timestamp=int(start))

        # Determine success: exit code 0 AND at least one host was scanned
        # If nmap exits 0 but scanned 0 hosts, something went wrong (e.g., bad target format)
        scan_success = proc.returncode == 0
        hosts_total = metadata.get("hosts_total", 0)
        stderr_output = proc.stderr
        
        if scan_success and hosts_total == 0 and "-sn" not in [t.value for t in args.scan_types]:
            # Port scan with 0 hosts likely means target parsing failed
            scan_success = False
            warning_msg = "WARNING: Nmap exited successfully but scanned 0 hosts. Target may not have been parsed correctly."
            stderr_output = f"{warning_msg}\n{proc.stderr}" if proc.stderr else warning_msg
            metadata["warning"] = "zero_hosts_scanned"

        return ToolResult(
            success=scan_success,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=stderr_output,
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
        tool_id="information_gathering.network_discovery.nmap",
        display_name="Nmap",
        category=ToolCategory.NETWORK_DISCOVERY,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="port_discovery",
                description="Scan TCP/UDP ports on hosts/ranges; returns open ports, services, OS hints; prefer for normal targeted scans, not for high-speed sweeps",
                output_indicators=["open", "filtered"],
            ),
            ToolCapability(
                name="service_detection",
                description="Identify running services",
                output_indicators=["version"],
            ),
            ToolCapability(
                name="os_detection",
                description="Detect operating system",
                output_indicators=["OS details"],
            ),
        ],
        required_services=[],
        target_protocols=["tcp", "udp"],
        execution_priority=9,
        parallel_compatible=True,
        max_concurrent_per_target=3,
        stealth_level=3,
        best_combined_with=["information_gathering.network_discovery.masscan"],
        estimated_runtime_minutes=10,
    )
)
