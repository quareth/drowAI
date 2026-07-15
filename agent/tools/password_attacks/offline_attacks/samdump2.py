"""SAMdump2 - Windows SAM Database Dumping Tool."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class OutputFormat(str, Enum):
    """Output format options for SAMdump2."""
    TEXT = "text"
    JSON = "json"
    XML = "xml"
    CSV = "csv"


class HashFormat(str, Enum):
    """Hash format options for SAMdump2."""
    LM = "lm"
    NTLM = "ntlm"
    LM_NTLM = "lm_ntlm"
    PWDUMP = "pwdump"
    JOHN = "john"


class SAMdump2Args(BaseToolArgs):
    """Arguments for the SAMdump2 tool."""
    
    output_format: OutputFormat = Field(
        default=OutputFormat.TEXT,
        description="Output format for the tool"
    )
    
    hash_format: HashFormat = Field(
        default=HashFormat.NTLM,
        description="Hash format for output"
    )
    
    verbose: bool = Field(
        default=False,
        description="Enable verbose output"
    )
    
    timeout: int = Field(
        default=300,
        description="Timeout in seconds for the operation",
        ge=30,
        le=1800
    )
    
    output_file: Optional[str] = Field(
        None,
        description="Output file path for hashes"
    )
    
    system_file: Optional[str] = Field(
        None,
        description="Path to SYSTEM file"
    )
    
    sam_file: Optional[str] = Field(
        None,
        description="Path to SAM file"
    )
    
    security_file: Optional[str] = Field(
        None,
        description="Path to SECURITY file"
    )
    
    bootkey: Optional[str] = Field(
        None,
        description="Boot key for decryption"
    )
    
    local: bool = Field(
        default=False,
        description="Extract from local system"
    )
    
    remote: bool = Field(
        default=False,
        description="Extract from remote system"
    )
    
    domain: bool = Field(
        default=False,
        description="Extract domain accounts"
    )
    
    local_accounts: bool = Field(
        default=True,
        description="Extract local accounts"
    )
    
    target: str = Field(
        ...,
        description="Target path or hostname (depends on execution mode)"
    )


def parse_samdump2_output(output_text: str) -> Dict[str, Any]:
    """Parse SAMdump2 output into structured metadata."""
    metadata: Dict[str, Any] = {
        "accounts_found": 0,
        "hashes_extracted": 0,
        "local_accounts": 0,
        "domain_accounts": 0,
        "disabled_accounts": 0,
        "locked_accounts": 0,
        "execution_status": "unknown"
    }
    
    try:
        lines = output_text.split('\n')
        for line in lines:
            line = line.strip()
            if ":" in line and len(line.split(":")) >= 3:
                # This looks like a hash line (username:rid:hash)
                metadata["hashes_extracted"] += 1
                metadata["accounts_found"] += 1
            if "local account" in line.lower():
                metadata["local_accounts"] += 1
            if "domain account" in line.lower():
                metadata["domain_accounts"] += 1
            if "disabled" in line.lower():
                metadata["disabled_accounts"] += 1
            if "locked" in line.lower():
                metadata["locked_accounts"] += 1
            if "completed" in line.lower():
                metadata["execution_status"] = "completed"
            elif "failed" in line.lower() or "error" in line.lower():
                metadata["execution_status"] = "failed"
    except Exception:
        metadata["execution_status"] = "parsing_error"
    
    return metadata


class SAMdump2Tool(BaseTool):
    """Run SAMdump2 for Windows SAM database dumping."""
    
    args_model = SAMdump2Args
    
    def build_command(self, args: SAMdump2Args) -> List[str]:
        cmd = ["samdump2"]

        if args.output_format != OutputFormat.TEXT:
            cmd.extend(["--format", args.output_format.value])
        cmd.extend(["--hash-format", args.hash_format.value])
        if args.verbose:
            cmd.append("--verbose")
        if args.output_file:
            cmd.extend(["--output", args.output_file])
        if args.system_file:
            cmd.extend(["--system", args.system_file])
        if args.sam_file:
            cmd.extend(["--sam", args.sam_file])
        if args.security_file:
            cmd.extend(["--security", args.security_file])
        if args.bootkey:
            cmd.extend(["--bootkey", args.bootkey])
        if args.local:
            cmd.append("--local")
        if args.remote:
            cmd.append("--remote")
        if args.domain:
            cmd.append("--domain")
        if not args.local_accounts:
            cmd.append("--no-local")

        cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: SAMdump2Args,
    ) -> Dict[str, Any]:
        metadata = parse_samdump2_output(stdout or "")
        metadata.update(
            {
                "hash_format": args.hash_format.value,
                "target": args.target,
                "exit_code": exit_code,
            }
        )
        if stderr:
            metadata["stderr"] = stderr
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: SAMdump2Args,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []
        if stdout and len(stdout) > 100:
            ts = int(timestamp or time.time())
            artifact_path = f"artifacts/samdump2_{ts}.txt"
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

    def run(self, args: SAMdump2Args) -> ToolResult:
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
                stderr="samdump2 command not found. Please ensure SAMdump2 is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        except Exception as exc:
            msg = f"Error executing samdump2: {exc}"
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


__all__ = ["SAMdump2Tool", "SAMdump2Args"]


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
        tool_id="password_attacks.offline_attacks.samdump2",
        display_name="SAMdump2",
        category=ToolCategory.PASSWORD_ATTACKS,
        applicable_phases=[PentestPhase.EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="offline_hash_extraction",
                description="Extract Windows NTLM and LM hashes from offline SAM, SECURITY, or SYSTEM hive files; returns hash:RID pairs; not for cracking — feeds john or hashcat.",
                output_indicators=["hash", "account"],
            ),
        ],
        required_services=[],
        target_protocols=["file"],
        execution_priority=4,
        parallel_compatible=False,
        stealth_level=2,
        estimated_runtime_minutes=10,
    )
)
