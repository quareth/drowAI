"""NTLMRelayX tool implementation for NTLM relay attacks and credential forwarding."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class RelayMode(str, Enum):
    """Supported NTLMRelayX modes."""
    
    HTTP = "http"
    SMB = "smb"
    LDAP = "ldap"
    MSSQL = "mssql"
    RPC = "rpc"
    WINRM = "winrm"
    EWS = "ews"
    SMTP = "smtp"


class RelayAction(str, Enum):
    """Supported relay actions."""
    
    DUMP = "dump"
    SHELL = "shell"
    EXEC = "exec"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    ADD_USER = "add-user"
    DEL_USER = "del-user"
    MODIFY_USER = "modify-user"
    QUERY = "query"


class NTLMRelayXArgs(BaseToolArgs):
    """Arguments for the NTLMRelayX tool."""
    
    mode: RelayMode = Field(
        RelayMode.SMB,
        description="Relay mode to use"
    )
    
    action: RelayAction = Field(
        RelayAction.DUMP,
        description="Action to perform after successful relay"
    )
    
    # Target options
    targets: Optional[List[str]] = Field(
        None,
        description="List of target hosts for relay"
    )
    target_file: Optional[str] = Field(
        None,
        description="File containing target hosts"
    )
    target: Optional[str] = Field(
        None,
        description="Single target host when targets list/file is not provided"
    )
    
    # Relay options
    relay_host: Optional[str] = Field(
        None,
        description="Host to relay credentials to"
    )
    relay_port: Optional[int] = Field(
        None,
        description="Port for relay connection"
    )
    
    # Authentication options
    username: Optional[str] = Field(
        None,
        description="Username for authentication"
    )
    password: Optional[str] = Field(
        None,
        description="Password for authentication"
    )
    domain: Optional[str] = Field(
        None,
        description="Domain for authentication"
    )
    hash: Optional[str] = Field(
        None,
        description="NTLM hash for authentication"
    )
    
    # Command options
    command: Optional[str] = Field(
        None,
        description="Command to execute after relay"
    )
    command_file: Optional[str] = Field(
        None,
        description="File containing commands to execute"
    )
    
    # File transfer options
    upload_file: Optional[str] = Field(
        None,
        description="File to upload after relay"
    )
    download_file: Optional[str] = Field(
        None,
        description="File to download after relay"
    )
    
    # User management options
    new_username: Optional[str] = Field(
        None,
        description="Username for user management operations"
    )
    new_password: Optional[str] = Field(
        None,
        description="Password for new user"
    )
    new_domain: Optional[str] = Field(
        None,
        description="Domain for new user"
    )
    
    # Output options
    output_file: Optional[str] = Field(
        None,
        description="Output file path"
    )
    log_file: Optional[str] = Field(
        None,
        description="Log file path"
    )
    
    # Execution options
    verbose: bool = Field(
        False,
        description="Enable verbose output"
    )
    debug: bool = Field(
        False,
        description="Enable debug mode"
    )
    quiet: bool = Field(
        False,
        description="Suppress output"
    )
    
    # Advanced options
    interface: Optional[str] = Field(
        None,
        description="Network interface to use"
    )
    port: Optional[int] = Field(
        None,
        description="Port to listen on"
    )
    timeout: int = Field(
        30,
        description="Maximum execution time in seconds"
    )


def parse_ntlmrelayx_output(output_text: str) -> Dict[str, Any]:
    """Parse NTLMRelayX output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "relay_attempts": [],
        "successful_relays": [],
        "failed_relays": [],
        "captured_credentials": [],
        "executed_commands": [],
        "summary": {
            "total_attempts": 0,
            "successful_relays": 0,
            "failed_relays": 0,
            "captured_creds": 0,
            "executed_commands": 0
        }
    }
    
    try:
        lines = output_text.split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Parse relay attempts
            if "relay" in line.lower() and "attempt" in line.lower():
                metadata["relay_attempts"].append({"raw_line": line})
                metadata["summary"]["total_attempts"] += 1
            
            # Parse successful relays
            elif "successful" in line.lower() and "relay" in line.lower():
                metadata["successful_relays"].append({"raw_line": line})
                metadata["summary"]["successful_relays"] += 1
            
            # Parse failed relays
            elif "failed" in line.lower() and "relay" in line.lower():
                metadata["failed_relays"].append({"raw_line": line})
                metadata["summary"]["failed_relays"] += 1
            
            # Parse captured credentials
            elif "hash" in line.lower() and ":" in line:
                if "ntlm" in line.lower() or "lm" in line.lower():
                    parts = line.split(":")
                    if len(parts) >= 3:
                        cred = {
                            "username": parts[0].strip(),
                            "domain": parts[1].strip() if len(parts) > 3 else "",
                            "hash": parts[-1].strip(),
                            "type": "NTLM" if "ntlm" in line.lower() else "LM"
                        }
                        metadata["captured_credentials"].append(cred)
                        metadata["summary"]["captured_creds"] += 1
            
            # Parse executed commands
            elif "executed" in line.lower() and "command" in line.lower():
                metadata["executed_commands"].append({"raw_line": line})
                metadata["summary"]["executed_commands"] += 1
    
    except Exception as e:
        metadata["parse_error"] = str(e)
    
    return metadata


class NTLMRelayXTool(BaseTool):
    """NTLMRelayX tool for NTLM relay attacks and credential forwarding."""
    
    args_model = NTLMRelayXArgs
    
    def build_command(self, args: NTLMRelayXArgs) -> List[str]:
        cmd = ["ntlmrelayx.py", "-m", args.mode.value, "-a", args.action.value]

        if args.targets:
            for target in args.targets:
                cmd.extend(["-t", target])
        elif args.target_file:
            cmd.extend(["-tf", args.target_file])
        elif args.target:
            cmd.extend(["-t", args.target])

        if args.relay_host:
            cmd.extend(["-r", args.relay_host])
        if args.relay_port:
            cmd.extend(["-rp", str(args.relay_port)])

        if args.username:
            cmd.extend(["-u", args.username])
        if args.password:
            cmd.extend(["-p", args.password])
        if args.domain:
            cmd.extend(["-d", args.domain])
        if args.hash:
            cmd.extend(["-H", args.hash])

        if args.command:
            cmd.extend(["-c", args.command])
        if args.command_file:
            cmd.extend(["-cf", args.command_file])

        if args.upload_file:
            cmd.extend(["-uf", args.upload_file])
        if args.download_file:
            cmd.extend(["-df", args.download_file])

        if args.new_username:
            cmd.extend(["-nu", args.new_username])
        if args.new_password:
            cmd.extend(["-np", args.new_password])
        if args.new_domain:
            cmd.extend(["-nd", args.new_domain])

        if args.output_file:
            cmd.extend(["-o", args.output_file])
        if args.log_file:
            cmd.extend(["-l", args.log_file])

        if args.verbose:
            cmd.append("-v")
        if args.debug:
            cmd.append("-d")
        if args.quiet:
            cmd.append("-q")

        if args.interface:
            cmd.extend(["-i", args.interface])
        if args.port:
            cmd.extend(["-p", str(args.port)])

        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: NTLMRelayXArgs,
    ) -> Dict[str, Any]:
        metadata = parse_ntlmrelayx_output(stdout or "")
        metadata.update(
            {
                "mode": args.mode.value,
                "action": args.action.value,
                "targets": args.targets or ([] if args.target is None else [args.target]),
                "exit_code": exit_code,
            }
        )

        # Mask secrets
        for credential in metadata.get("captured_credentials", []):
            if "hash" in credential:
                credential["hash"] = "***"
        if stderr:
            metadata["stderr"] = stderr
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: NTLMRelayXArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []
        if stdout and len(stdout) > 100:
            ts = int(timestamp or time.time())
            artifact_path = f"artifacts/ntlmrelayx_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except OSError:
                pass

        if args.output_file and os.path.exists(args.output_file):
            artifacts.append(args.output_file)
        if args.log_file and os.path.exists(args.log_file):
            artifacts.append(args.log_file)
        return artifacts

    def run(self, args: NTLMRelayXArgs) -> ToolResult:
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
                stderr="ntlmrelayx.py command not found. Please ensure Impacket/NTLMRelayX is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        except Exception as exc:
            msg = f"Error executing ntlmrelayx.py: {exc}"
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


__all__ = ["NTLMRelayXTool", "NTLMRelayXArgs"]


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
        tool_id="password_attacks.passing_the_hash.ntlmrelayx",
        display_name="NTLMRelayX",
        category=ToolCategory.PASSWORD_ATTACKS,
        applicable_phases=[PentestPhase.EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="ntlm_relay",
                description="Relay captured NTLM authentication to remote services (SMB, LDAP, HTTP, MSSQL, EWS) with post-relay actions (dump, shell, user-add); not for capturing the initial NTLM exchange.",
                output_indicators=["relay_success", "hash"],
            ),
        ],
        required_services=["smb", "ldap", "http", "mssql"],
        target_protocols=["tcp"],
        execution_priority=7,
        parallel_compatible=False,
        stealth_level=3,
        estimated_runtime_minutes=20,
    )
)