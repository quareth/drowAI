"""Patator - Multi-purpose brute-forcer for various protocols and services."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class PatatorModule(str, Enum):
    """Supported Patator modules."""
    FTP_LOGIN = "ftp_login"
    SSH_LOGIN = "ssh_login"
    TELNET_LOGIN = "telnet_login"
    SMTP_LOGIN = "smtp_login"
    SMTP_VRFY = "smtp_vrfy"
    SMTP_RCPT = "smtp_rcpt"
    HTTP_FUZZ = "http_fuzz"
    RDP_LOGIN = "rdp_login"
    MYSQL_LOGIN = "mysql_login"
    POSTGRES_LOGIN = "postgres_login"
    SMB_LOGIN = "smb_login"
    VNC_LOGIN = "vnc_login"
    SNMP_LOGIN = "snmp_login"
    DNS_FORWARD = "dns_forward"
    DNS_REVERSE = "dns_reverse"
    LDAP_LOGIN = "ldap_login"
    SSH_KEY = "ssh_key"
    HTTP_FORMAUTH = "http_formauth"
    HTTP_BASIC = "http_basic"
    HTTP_DIGEST = "http_digest"
    HTTP_NTLM = "http_ntlm"
    HTTP_PROXY = "http_proxy"
    IMAP_LOGIN = "imap_login"
    POP3_LOGIN = "pop3_login"
    REXEC_LOGIN = "rexec_login"
    RLOGIN_LOGIN = "rlogin_login"
    RSH_LOGIN = "rsh_login"
    SMB_LS = "smb_ls"
    SMB_SHARE = "smb_share"
    SNMP_LOGIN_PW_SPRAY = "snmp_login_pw_spray"
    VNC_AUTH = "vnc_auth"


class OutputFormat(str, Enum):
    """Output format options for Patator."""
    TEXT = "text"
    JSON = "json"
    CSV = "csv"
    XML = "xml"


def parse_patator_output(output_text: str) -> Dict[str, Any]:
    """Parse Patator output into structured metadata."""
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
            if "SUCCESS" in line or "found" in line.lower():
                metadata["successful_logins"] += 1
                # Extract credentials if present
                if ":" in line:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        metadata["found_credentials"].append({
                            "username": parts[0].strip(),
                            "password": parts[1].strip()
                        })
            elif "FAIL" in line or "failed" in line.lower():
                metadata["failed_attempts"] += 1
            elif "attempt" in line.lower():
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


class PatatorArgs(BaseToolArgs):
    """Arguments for the Patator tool."""
    
    module: PatatorModule = Field(
        ...,
        description="Patator module to use for the attack"
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
    
    extra_args: List[str] = Field(
        default_factory=list,
        description="Additional command line arguments"
    )


class PatatorTool(BaseTool):
    """Run Patator brute-force attacks against various protocols and services."""
    
    args_model = PatatorArgs
    
    def build_command(self, args: PatatorArgs) -> List[str]:
        cmd: List[str] = ["patator", args.module.value]

        cmd.append(f"host={args.host}")
        if args.port:
            cmd.append(f"port={args.port}")

        if args.user:
            cmd.append(f"user={args.user}")
        elif args.user_file:
            cmd.append("user=FILE0")
            cmd.extend(["0", args.user_file])

        if args.password:
            cmd.append(f"password={args.password}")
        elif args.password_file:
            cmd.append("password=FILE1")
            cmd.extend(["1", args.password_file])

        if args.output_format != OutputFormat.TEXT:
            cmd.extend(["-x", f"ignore:code={args.output_format.value}"])
        if args.verbose:
            cmd.append("-v")
        if args.output_file:
            cmd.extend(["-o", args.output_file])
        if args.max_attempts:
            cmd.extend(["-x", f"ignore:attempts={args.max_attempts}"])
        if args.delay:
            cmd.extend(["-x", f"ignore:delay={args.delay}"])
        if args.extra_args:
            cmd.extend(args.extra_args)

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: PatatorArgs,
    ) -> Dict[str, Any]:
        metadata = parse_patator_output(stdout or "")
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
        args: PatatorArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []
        if stdout and len(stdout) > 100:
            ts = int(timestamp or time.time())
            artifact_path = f"artifacts/patator_{ts}.txt"
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

    def run(self, args: PatatorArgs) -> ToolResult:
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
                stderr="patator command not found. Please ensure Patator is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        except Exception as exc:
            msg = f"Error executing patator: {exc}"
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


__all__ = ["PatatorTool", "PatatorArgs"]


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
        tool_id="password_attacks.online_attacks.patator",
        display_name="Patator",
        category=ToolCategory.PASSWORD_ATTACKS,
        applicable_phases=[PentestPhase.EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="online_password_bruteforce",
                description="Modular brute-force and protocol fuzzing (FTP, SSH, SMTP, HTTP, RDP, SMB, DNS) with custom payload combinations; returns valid credentials; use for protocol-specific attacks.",
                output_indicators=["success", "credential", "login"],
            ),
        ],
        required_services=["ssh", "ftp", "http", "smtp", "rdp"],
        target_protocols=["tcp"],
        execution_priority=5,
        parallel_compatible=False,
        stealth_level=2,
        estimated_runtime_minutes=15,
    )
)
