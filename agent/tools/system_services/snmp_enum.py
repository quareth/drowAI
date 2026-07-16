"""Provide SNMP enumeration arguments and command execution."""

import os
import subprocess
import time
import re
from typing import Dict, Any, Optional, List
from enum import Enum
from pydantic import Field

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult

ARTIFACT_MIN_CHARS = 120
DEFAULT_OID = "1.3.6.1.2.1"

class SNMPEnumVersion(str, Enum):
    """SNMP versions supported"""
    V1 = "1"
    V2C = "2c"
    V3 = "3"

class SNMPEnumSecurityLevel(str, Enum):
    """SNMPv3 security levels"""
    NO_AUTH_NO_PRIV = "noAuthNoPriv"
    AUTH_NO_PRIV = "authNoPriv"
    AUTH_PRIV = "authPriv"

class SNMPEnumAuthProtocol(str, Enum):
    """SNMPv3 authentication protocols"""
    MD5 = "md5"
    SHA = "sha"
    SHA224 = "sha224"
    SHA256 = "sha256"
    SHA384 = "sha384"
    SHA512 = "sha512"

class SNMPEnumPrivProtocol(str, Enum):
    """SNMPv3 privacy protocols"""
    DES = "des"
    AES = "aes"
    AES192 = "aes192"
    AES256 = "aes256"

class SNMPEnumArgs(BaseToolArgs):
    """Arguments for SNMP enumeration tool"""
    version: SNMPEnumVersion = Field(default=SNMPEnumVersion.V2C, description="SNMP version to use")
    community: str = Field(default="public", description="SNMP community string")
    username: Optional[str] = Field(default=None, description="SNMPv3 username")
    auth_password: Optional[str] = Field(default=None, description="SNMPv3 authentication password")
    auth_protocol: Optional[SNMPEnumAuthProtocol] = Field(default=None, description="SNMPv3 authentication protocol")
    priv_protocol: Optional[SNMPEnumPrivProtocol] = Field(default=None, description="SNMPv3 privacy protocol")
    priv_password: Optional[str] = Field(default=None, description="SNMPv3 privacy password")
    security_level: SNMPEnumSecurityLevel = Field(
        default=SNMPEnumSecurityLevel.NO_AUTH_NO_PRIV,
        description="SNMPv3 security level",
    )
    retries: Optional[int] = Field(default=None, description="Number of retries (-r)")
    verbose: bool = Field(default=False, description="Enable verbose output")
    oid: str = Field(default=DEFAULT_OID, description="OID subtree to query")

def parse_snmp_enum_output(output_text: str) -> Dict[str, Any]:
    """Parse SNMP enumeration command output and extract structured information."""
    result = {
        "target_info": {},
        "system_info": {},
        "interfaces": [],
        "processes": [],
        "services": [],
        "software": [],
        "shares": [],
        "drives": [],
        "registry": {},
        "users": [],
        "routes": [],
        "tcp_connections": [],
        "udp_connections": [],
        "arp_table": [],
        "ip_addresses": [],
        "printers": [],
        "cisco_info": {},
        "windows_info": {},
        "linux_info": {},
        "snmp_info": {},
        "walk_results": [],
        "errors": [],
        "metadata": {}
    }
    
    try:
        for line in output_text.splitlines():
            line = line.strip()
            if not line:
                continue
            match = re.match(r"^(\S+)\s*=\s*(.+)$", line)
            if match:
                result["walk_results"].append(
                    {"oid": match.group(1), "value": match.group(2).strip()}
                )

        error_lines = re.findall(r"ERROR:\s*(.+)", output_text, re.IGNORECASE)
        result["errors"].extend(error_lines)

        result["metadata"] = {
            "total_walk_results": len(result["walk_results"]),
            "has_errors": len(result["errors"]) > 0
        }
        
    except Exception as e:
        result["errors"].append(f"Error parsing output: {str(e)}")
    
    return result

class SNMPEnumTool(BaseTool):
    """SNMP Enumeration Tool for network device enumeration."""
    name: str = "snmp_enum"
    description: str = "SNMP enumeration tool for network device reconnaissance and enumeration"
    version: str = "1.0.0"
    args_model = SNMPEnumArgs

    def build_command(self, args: SNMPEnumArgs) -> List[str]:
        cmd = ["snmpwalk", "-v", args.version.value]
        if args.version in {SNMPEnumVersion.V1, SNMPEnumVersion.V2C}:
            cmd.extend(["-c", args.community])
        else:
            if args.username:
                cmd.extend(["-u", args.username])
            cmd.extend(["-l", args.security_level.value])
            if args.auth_protocol:
                cmd.extend(["-a", args.auth_protocol.value])
            if args.auth_password:
                cmd.extend(["-A", args.auth_password])
            if args.priv_protocol:
                cmd.extend(["-x", args.priv_protocol.value])
            if args.priv_password:
                cmd.extend(["-X", args.priv_password])

        if args.retries is not None:
            cmd.extend(["-r", str(args.retries)])
        if args.verbose:
            cmd.append("-v")
        cmd.extend(["-t", str(args.timeout)])
        cmd.append(args.target)
        cmd.append(args.oid)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: SNMPEnumArgs,
    ) -> Dict[str, Any]:
        metadata = parse_snmp_enum_output(stdout or "")
        metadata["exit_code"] = exit_code
        if stderr:
            metadata["stderr"] = stderr[:2000]
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: SNMPEnumArgs,
        timestamp: Optional[int] = None,
        stderr: str | None = None,
    ) -> List[str]:
        combined = "\n".join([(stdout or "").strip(), (stderr or "").strip()]).strip()
        if not combined or len(combined) < ARTIFACT_MIN_CHARS:
            return []
        os.makedirs("artifacts", exist_ok=True)
        ts = int(timestamp or time.time())
        path = f"artifacts/snmp_enum_{ts}.txt"
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(combined + "\n")
        except OSError:
            return []
        return [path]

    def run(self, args: SNMPEnumArgs) -> ToolResult:
        start = time.time()
        try:
            cmd = self.build_command(args)
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
        except FileNotFoundError:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="snmpwalk command not found. Ensure snmp is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )

        metadata = self.parse_output(
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.returncode,
            args=args,
        )
        artifacts = self.create_artifacts(
            proc.stdout, args=args, timestamp=int(start), stderr=proc.stderr
        )

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
from agent.tools.enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="system_services.snmp_enum",
        display_name="snmpwalk",
        category=ToolCategory.SYSTEM_SERVICES,
        applicable_phases=[PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="snmp_enumeration",
                description="Walks SNMP OIDs to enumerate device information.",
                output_indicators=["oid", "snmp"],
            ),
        ],
        required_services=["snmp"],
        target_protocols=["udp"],
        execution_priority=6,
        parallel_compatible=True,
        stealth_level=3,
        estimated_runtime_minutes=3,
    )
)
