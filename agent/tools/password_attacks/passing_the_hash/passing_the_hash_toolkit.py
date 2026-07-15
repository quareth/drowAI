"""Passing The Hash Toolkit - Comprehensive tool for hash-based authentication attacks."""

from __future__ import annotations
import os
import re
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult

class AttackMode(str, Enum):
    """Passing The Hash Toolkit attack modes."""
    SMB = "smb"
    RDP = "rdp"
    SSH = "ssh"
    WINRM = "winrm"
    LDAP = "ldap"
    ALL = "all"

class HashType(str, Enum):
    """Passing The Hash Toolkit supported hash types."""
    NTLM = "ntlm"
    LM = "lm"
    NTLMV2 = "ntlmv2"
    SHA1 = "sha1"
    SHA256 = "sha256"
    MD5 = "md5"
    ALL = "all"

class OutputFormat(str, Enum):
    """Passing The Hash Toolkit output format options."""
    TEXT = "text"
    JSON = "json"
    XML = "xml"
    CSV = "csv"

class ProtocolType(str, Enum):
    """Passing The Hash Toolkit protocol types."""
    SMB = "smb"
    RDP = "rdp"
    SSH = "ssh"
    WINRM = "winrm"
    LDAP = "ldap"
    HTTP = "http"
    HTTPS = "https"

class PassingTheHashToolkitArgs(BaseToolArgs):
    """Arguments for the Passing The Hash Toolkit tool."""
    target: str = Field(..., description="Target host or IP address")
    username: str = Field(..., description="Username for authentication")
    hash_value: str = Field(..., description="Hash value to use for authentication")
    attack_mode: AttackMode = Field(AttackMode.SMB, description="Attack mode to use")
    hash_type: HashType = Field(HashType.NTLM, description="Type of hash being used")
    protocol: ProtocolType = Field(ProtocolType.SMB, description="Protocol to use for connection")
    output_format: OutputFormat = Field(OutputFormat.TEXT, description="Output format for results")
    output_file: Optional[str] = Field(None, description="Path to save output results")
    port: Optional[int] = Field(None, ge=1, le=65535, description="Port number for connection")
    timeout: int = Field(30, ge=5, le=300, description="Connection timeout in seconds")
    verbose: bool = Field(False, description="Enable verbose output")
    quiet: bool = Field(False, description="Suppress all output except for errors")
    include_metadata: bool = Field(True, description="Include metadata in output")
    domain: Optional[str] = Field(None, description="Domain name for authentication")
    service: Optional[str] = Field(None, description="Service name to target")
    command: Optional[str] = Field(None, description="Command to execute after successful authentication")
    session_name: Optional[str] = Field(None, description="Session name for persistent connections")
    brute_force: bool = Field(False, description="Enable brute force mode")
    wordlist: Optional[str] = Field(None, description="Wordlist file for brute force attacks")
    threads: int = Field(5, ge=1, le=50, description="Number of threads to use")
    common_timeout: int = Field(600, ge=60, le=3600, description="Timeout in seconds for the entire operation")

def parse_passing_the_hash_toolkit_output(output_text: str) -> Dict[str, Any]:
    """Parse Passing The Hash Toolkit output into structured metadata."""
    metadata: Dict[str, Any] = {"attacks": [], "summary": {}, "sessions": {}}
    try:
        lines = output_text.strip().split('\n')

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # Parse successful attacks
            if "success" in line.lower() or "authenticated" in line.lower():
                attack_data = {
                    "status": "success",
                    "target": "",
                    "username": "",
                    "protocol": "",
                    "timestamp": ""
                }

                # Extract target information
                target_match = re.search(r'(\d+\.\d+\.\d+\.\d+|\w+\.\w+)', line)
                if target_match:
                    attack_data["target"] = target_match.group(1)

                # Extract username
                user_match = re.search(r'user[:\s]+(\w+)', line.lower())
                if user_match:
                    attack_data["username"] = user_match.group(1)

                # Extract protocol
                protocol_match = re.search(r'(smb|rdp|ssh|winrm|ldap)', line.lower())
                if protocol_match:
                    attack_data["protocol"] = protocol_match.group(1)

                metadata["attacks"].append(attack_data)

            # Parse failed attacks
            elif "failed" in line.lower() or "denied" in line.lower():
                attack_data = {
                    "status": "failed",
                    "target": "",
                    "username": "",
                    "reason": line
                }

                # Extract target information
                target_match = re.search(r'(\d+\.\d+\.\d+\.\d+|\w+\.\w+)', line)
                if target_match:
                    attack_data["target"] = target_match.group(1)

                metadata["attacks"].append(attack_data)

            # Parse session information
            elif "session" in line.lower():
                session_match = re.search(r'session[:\s]+(\w+)', line.lower())
                if session_match:
                    metadata["sessions"]["active_session"] = session_match.group(1)

            # Parse summary information
            elif "attempts" in line.lower():
                attempts_match = re.search(r'(\d+)\s+attempts', line.lower())
                if attempts_match:
                    metadata["summary"]["total_attempts"] = int(attempts_match.group(1))

            elif "successful" in line.lower():
                success_match = re.search(r'(\d+)\s+successful', line.lower())
                if success_match:
                    metadata["summary"]["successful_attacks"] = int(success_match.group(1))

            elif "failed" in line.lower():
                failed_match = re.search(r'(\d+)\s+failed', line.lower())
                if failed_match:
                    metadata["summary"]["failed_attacks"] = int(failed_match.group(1))

            # Parse execution time
            elif "time" in line.lower():
                time_match = re.search(r'(\d+\.?\d*)\s*(seconds?|minutes?)', line.lower())
                if time_match:
                    metadata["summary"]["execution_time"] = time_match.group(0)

        # Update summary
        metadata["summary"]["total_attacks"] = len(metadata["attacks"])
        metadata["summary"]["successful"] = len([a for a in metadata["attacks"] if a["status"] == "success"])
        metadata["summary"]["failed"] = len([a for a in metadata["attacks"] if a["status"] == "failed"])

    except Exception as e:
        metadata["parse_error"] = str(e)

    return metadata

class PassingTheHashToolkitTool(BaseTool):
    """Passing The Hash Toolkit - Comprehensive tool for hash-based authentication attacks."""
    args_model = PassingTheHashToolkitArgs

    def build_command(self, args: PassingTheHashToolkitArgs) -> List[str]:
        cmd = ["pth-toolkit", "-t", args.target, "-u", args.username, "-H", args.hash_value]

        if args.attack_mode != AttackMode.SMB:
            cmd.extend(["-m", args.attack_mode.value])
        if args.hash_type != HashType.NTLM:
            cmd.extend(["-T", args.hash_type.value])
        if args.protocol != ProtocolType.SMB:
            cmd.extend(["-p", args.protocol.value])
        if args.output_format != OutputFormat.TEXT:
            cmd.extend(["-f", args.output_format.value])
        if args.output_file:
            cmd.extend(["-o", args.output_file])
        if args.port:
            cmd.extend(["-P", str(args.port)])
        if args.timeout != 30:
            cmd.extend(["--timeout", str(args.timeout)])
        if args.verbose:
            cmd.append("-v")
        if args.quiet:
            cmd.append("-q")
        if not args.include_metadata:
            cmd.append("--no-metadata")
        if args.domain:
            cmd.extend(["-d", args.domain])
        if args.service:
            cmd.extend(["-s", args.service])
        if args.command:
            cmd.extend(["-c", args.command])
        if args.session_name:
            cmd.extend(["-S", args.session_name])
        if args.brute_force:
            cmd.append("--brute-force")
        if args.wordlist:
            cmd.extend(["-w", args.wordlist])
        if args.threads != 5:
            cmd.extend(["--threads", str(args.threads)])
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: PassingTheHashToolkitArgs,
    ) -> Dict[str, Any]:
        metadata = parse_passing_the_hash_toolkit_output(stdout or "")
        metadata.update(
            {
                "target": args.target,
                "protocol": args.protocol.value,
                "attack_mode": args.attack_mode.value,
                "hash_type": args.hash_type.value,
                "exit_code": exit_code,
            }
        )
        if stderr:
            metadata["stderr"] = stderr

        # Mask sensitive hash values
        metadata["hash_masked"] = True
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: PassingTheHashToolkitArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []
        if stdout and len(stdout) > 100:
            ts = int(timestamp or time.time())
            artifact_path = f"artifacts/passing_the_hash_toolkit_{ts}.txt"
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

    def run(self, args: PassingTheHashToolkitArgs) -> ToolResult:
        start = time.time()
        try:
            cmd = self.build_command(args)
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.common_timeout,
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
                stderr="pth-toolkit command not found. Please ensure the toolkit is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        except Exception as exc:
            msg = f"Error executing pth-toolkit: {exc}"
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


__all__ = ["PassingTheHashToolkitTool", "PassingTheHashToolkitArgs"]


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
        tool_id="password_attacks.passing_the_hash.passing_the_hash_toolkit",
        display_name="Passing The Hash Toolkit",
        category=ToolCategory.PASSWORD_ATTACKS,
        applicable_phases=[PentestPhase.EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="hash_based_auth",
                description="Authenticate to Windows or Unix services (SMB, RDP, SSH, WinRM, LDAP) with NTLM hashes instead of passwords; returns auth result and optional command output.",
                output_indicators=["authenticated", "hash"],
            ),
        ],
        required_services=["smb", "rdp", "ssh", "winrm", "ldap"],
        target_protocols=["tcp"],
        execution_priority=6,
        parallel_compatible=False,
        stealth_level=3,
        estimated_runtime_minutes=15,
    )
)
