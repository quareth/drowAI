"""Crowbar - Network authentication brute-forcing tool."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class CrowbarModule(str, Enum):
    """Supported Crowbar modules."""
    SSH = "ssh"
    RDP = "rdp"
    VNC = "vnc"
    WINRM = "winrm"
    HTTP = "http"
    HTTP_BASIC = "http_basic"
    HTTP_DIGEST = "http_digest"
    HTTP_NTLM = "http_ntlm"
    HTTP_FORMAUTH = "http_formauth"
    HTTP_PROXY = "http_proxy"
    SMB = "smb"
    LDAP = "ldap"
    MYSQL = "mysql"
    POSTGRES = "postgres"
    MONGODB = "mongodb"
    REDIS = "redis"
    ELASTIC = "elastic"
    CASSANDRA = "cassandra"
    RABBITMQ = "rabbitmq"
    FTP = "ftp"
    SFTP = "sftp"
    TELNET = "telnet"
    SMTP = "smtp"
    IMAP = "imap"
    POP3 = "pop3"
    SNMP = "snmp"


class OutputFormat(str, Enum):
    """Output format options for Crowbar."""
    TEXT = "text"
    JSON = "json"
    CSV = "csv"


def parse_crowbar_output(output_text: str) -> Dict[str, Any]:
    """Parse Crowbar output into structured metadata."""
    metadata: Dict[str, Any] = {
        "successful_logins": 0,
        "failed_attempts": 0,
        "total_attempts": 0,
        "found_credentials": [],
        "execution_status": "unknown"
    }
    
    try:
        lines = output_text.split('\n')
        for line in lines:
            line = line.strip()
            if "SUCCESS" in line or "found" in line.lower() or "valid" in line.lower():
                metadata["successful_logins"] += 1
                # Extract credentials if present
                if ":" in line:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        metadata["found_credentials"].append({
                            "username": parts[0].strip(),
                            "password": parts[1].strip()
                        })
            elif "FAIL" in line or "failed" in line.lower() or "invalid" in line.lower():
                metadata["failed_attempts"] += 1
            elif "attempt" in line.lower() or "tried" in line.lower():
                metadata["total_attempts"] += 1
                
        if metadata["successful_logins"] > 0:
            metadata["execution_status"] = "success"
        elif metadata["failed_attempts"] > 0:
            metadata["execution_status"] = "failed"
        else:
            metadata["execution_status"] = "no_attempts"
            
    except Exception:
        metadata["execution_status"] = "parsing_error"
    
    return metadata


class CrowbarArgs(BaseToolArgs):
    """Arguments for the Crowbar tool."""
    
    module: CrowbarModule = Field(
        ...,
        description="Crowbar module to use for the attack"
    )
    
    host: str = Field(
        ...,
        description="Target host to attack"
    )
    
    port: Optional[int] = Field(
        None,
        description="Target port (if not default for the module)"
    )
    
    user_file: Optional[str] = Field(
        None,
        description="Path to username wordlist file"
    )
    
    password_file: Optional[str] = Field(
        None,
        description="Path to password wordlist file"
    )
    
    user: Optional[str] = Field(
        None,
       description="Single username to test"
    )
    
    password: Optional[str] = Field(
        None,
        description="Single password to test"
    )
    
    output_format: OutputFormat = Field(
        default=OutputFormat.TEXT,
        description="Output format for results"
    )
    
    verbose: bool = Field(
        default=False,
        description="Enable verbose output"
    )
    
    timeout: int = Field(
        default=300,
        description="Timeout in seconds for the operation",
        ge=60,
        le=3600
    )
    
    output_file: Optional[str] = Field(
        None,
        description="Output file path for results"
    )
    
    max_attempts: Optional[int] = Field(
        None,
        description="Maximum number of attempts before stopping"
    )
    
    delay: Optional[float] = Field(
        None,
        description="Delay between attempts in seconds"
    )
    
    threads: Optional[int] = Field(
        None,
        description="Number of threads to use",
        ge=1,
        le=100
    )
    
    ssl: bool = Field(
        default=False,
        description="Use SSL/TLS for the connection"
    )
    
    domain: Optional[str] = Field(
        None,
        description="Domain for authentication (for Windows services)"
    )
    
    extra_args: List[str] = Field(
        default_factory=list,
        description="Additional command line arguments"
    )


class CrowbarTool(BaseTool):
    """Run Crowbar brute-force attacks against various network services."""

    args_model = CrowbarArgs

    def build_command(self, args: CrowbarArgs) -> List[str]:
        cmd = ["crowbar", "-b", args.module.value, "-s", args.host]

        if args.port:
            cmd.extend(["-p", str(args.port)])

        # Username and password inputs
        if args.user:
            cmd.extend(["-U", args.user])
        elif args.user_file:
            cmd.extend(["-U", args.user_file])

        if args.password:
            cmd.extend(["-C", args.password])
        elif args.password_file:
            cmd.extend(["-C", args.password_file])

        if args.output_format != OutputFormat.TEXT:
            cmd.extend(["-f", args.output_format.value])

        if args.verbose:
            cmd.append("-v")

        if args.output_file:
            cmd.extend(["-o", args.output_file])
        if args.max_attempts:
            cmd.extend(["-m", str(args.max_attempts)])
        if args.delay:
            cmd.extend(["-d", str(args.delay)])
        if args.threads:
            cmd.extend(["-t", str(args.threads)])
        if args.ssl:
            cmd.append("--ssl")
        if args.domain:
            cmd.extend(["--domain", args.domain])
        if args.extra_args:
            cmd.extend(args.extra_args)

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: CrowbarArgs,
    ) -> Dict[str, Any]:
        metadata = parse_crowbar_output(stdout or "")
        metadata.update(
            {
                "module": args.module.value,
                "host": args.host,
                "port": args.port,
                "total_lines": len(stdout.splitlines()) if stdout else 0,
                "exit_code": exit_code,
            }
        )

        for credential in metadata.get("found_credentials", []):
            if "password" in credential:
                credential["password"] = "***"
        if stderr:
            metadata["stderr"] = stderr
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: CrowbarArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []

        if stdout and len(stdout) > 100:
            ts = int(timestamp or time.time())
            artifact_path = f"artifacts/crowbar_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except OSError:
                pass

        if args.output_file and os.path.exists(args.output_file):
            artifacts.append(args.output_file)

        return artifacts

    def run(self, args: CrowbarArgs) -> ToolResult:
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
                stderr="crowbar command not found. Please ensure Crowbar is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        except Exception as exc:
            msg = f"Error executing crowbar: {exc}"
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=msg,
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
        artifacts = self.create_artifacts(proc.stdout, args=args, timestamp=int(start))

        return ToolResult(
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start,
        )


__all__ = ["CrowbarTool", "CrowbarArgs"]


# ---------------------------------------------------------------------------
# Tool Metadata Registration
# ---------------------------------------------------------------------------
from ...enhanced_metadata_registry import (  # noqa: E402
    EnhancedToolMetadata,
    PentestPhase,
    ToolCapability,
    ToolCategory,
    register_enhanced_tool_metadata,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="password_attacks.online_attacks.crowbar",
        display_name="Crowbar",
        category=ToolCategory.PASSWORD_ATTACKS,
        applicable_phases=[PentestPhase.EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="online_password_bruteforce",
                description="Brute-force remote service credentials (RDP, SSH, VNC, OpenVPN) with username and password lists; returns valid pairs; use for protocols hydra covers poorly.",
                output_indicators=["success", "credential", "login"],
            ),
        ],
        required_services=["rdp", "ssh", "vnc", "http", "smb"],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=False,
        stealth_level=2,
        estimated_runtime_minutes=15,
    )
)
