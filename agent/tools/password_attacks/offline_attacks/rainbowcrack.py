"""RainbowCrack - Rainbow Table Password Cracking Tool."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class HashAlgorithm(str, Enum):
    """RainbowCrack hash algorithms."""
    LM = "lm"
    NTLM = "ntlm"
    MD5 = "md5"
    SHA1 = "sha1"
    SHA256 = "sha256"
    SHA512 = "sha512"
    RIPEMD160 = "ripemd160"
    WHIRLPOOL = "whirlpool"


class Operation(str, Enum):
    """RainbowCrack operations."""
    GENERATE_TABLE = "generate_table"
    SORT_TABLE = "sort_table"
    MERGE_TABLE = "merge_table"
    CRACK_HASH = "crack_hash"
    CRACK_FILE = "crack_file"
    VERIFY_TABLE = "verify_table"
    SHOW_TABLE = "show_table"


class OutputFormat(str, Enum):
    """Output format options for RainbowCrack."""
    TEXT = "text"
    JSON = "json"
    XML = "xml"


class RainbowCrackArgs(BaseToolArgs):
    """Arguments for the RainbowCrack tool."""
    
    operation: Operation = Field(
        default=Operation.CRACK_HASH,
        description="RainbowCrack operation to perform"
    )
    
    hash_algorithm: HashAlgorithm = Field(
        default=HashAlgorithm.NTLM,
        description="Hash algorithm to use"
    )
    
    output_format: OutputFormat = Field(
        default=OutputFormat.TEXT,
        description="Output format for the tool"
    )
    
    verbose: bool = Field(
        default=False,
        description="Enable verbose output"
    )
    
    timeout: int = Field(
        default=1800,
        description="Timeout in seconds for the operation",
        ge=60,
        le=7200
    )
    
    table_path: Optional[str] = Field(
        None,
        description="Path to rainbow table file"
    )
    
    table_dir: Optional[str] = Field(
        None,
        description="Directory containing rainbow tables"
    )
    
    target: str = Field(
        ...,
        description="Hash string or input file, depending on the selected operation"
    )
    
    charset: Optional[str] = Field(
        None,
        description="Character set for table generation"
    )
    
    min_length: Optional[int] = Field(
        None,
        description="Minimum password length",
        ge=1,
        le=20
    )
    
    max_length: Optional[int] = Field(
        None,
        description="Maximum password length",
        ge=1,
        le=20
    )
    
    chain_length: Optional[int] = Field(
        None,
        description="Chain length for table generation",
        ge=1000,
        le=1000000
    )
    
    chain_count: Optional[int] = Field(
        None,
        description="Number of chains to generate",
        ge=1000,
        le=10000000
    )


def parse_rainbowcrack_output(output_text: str) -> Dict[str, Any]:
    """Parse RainbowCrack output into structured metadata."""
    metadata: Dict[str, Any] = {
        "operation": "unknown",
        "hash_algorithm": "unknown",
        "passwords_cracked": 0,
        "tables_processed": 0,
        "chains_generated": 0,
        "execution_status": "unknown"
    }
    
    try:
        lines = output_text.split('\n')
        for line in lines:
            line = line.strip()
            if "operation:" in line.lower():
                metadata["operation"] = line.split(":")[-1].strip()
            if "hash algorithm:" in line.lower():
                metadata["hash_algorithm"] = line.split(":")[-1].strip()
            if "password cracked" in line.lower() or "hash cracked" in line.lower():
                metadata["passwords_cracked"] += 1
            if "table processed" in line.lower() or "table loaded" in line.lower():
                metadata["tables_processed"] += 1
            if "chain generated" in line.lower():
                metadata["chains_generated"] += 1
            if "completed" in line.lower():
                metadata["execution_status"] = "completed"
            elif "failed" in line.lower() or "error" in line.lower():
                metadata["execution_status"] = "failed"
    except Exception:
        metadata["execution_status"] = "parsing_error"
    
    return metadata


class RainbowCrackTool(BaseTool):
    """Run RainbowCrack for rainbow table password cracking."""
    
    args_model = RainbowCrackArgs
    
    def build_command(self, args: RainbowCrackArgs) -> List[str]:
        cmd = ["rcrack"]

        cmd.extend(["--operation", args.operation.value])
        cmd.extend(["--algorithm", args.hash_algorithm.value])

        if args.output_format != OutputFormat.TEXT:
            cmd.extend(["--output", args.output_format.value])
        if args.verbose:
            cmd.append("--verbose")
        if args.table_path:
            cmd.extend(["--table", args.table_path])
        if args.table_dir:
            cmd.extend(["--table-dir", args.table_dir])
        if args.charset:
            cmd.extend(["--charset", args.charset])
        if args.min_length:
            cmd.extend(["--min-length", str(args.min_length)])
        if args.max_length:
            cmd.extend(["--max-length", str(args.max_length)])
        if args.chain_length:
            cmd.extend(["--chain-length", str(args.chain_length)])
        if args.chain_count:
            cmd.extend(["--chain-count", str(args.chain_count)])

        cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: RainbowCrackArgs,
    ) -> Dict[str, Any]:
        metadata = parse_rainbowcrack_output(stdout or "")
        metadata.update(
            {
                "operation": args.operation.value,
                "hash_algorithm": args.hash_algorithm.value,
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
        args: RainbowCrackArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []
        if stdout and len(stdout) > 100:
            ts = int(timestamp or time.time())
            artifact_path = f"artifacts/rainbowcrack_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except OSError:
                pass
        return artifacts

    def run(self, args: RainbowCrackArgs) -> ToolResult:
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
                stderr="rcrack command not found. Please ensure RainbowCrack is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        except Exception as exc:
            msg = f"Error executing rainbowcrack: {exc}"
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


__all__ = ["RainbowCrackTool", "RainbowCrackArgs"]


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
        tool_id="password_attacks.offline_attacks.rainbowcrack",
        display_name="RainbowCrack",
        category=ToolCategory.PASSWORD_ATTACKS,
        applicable_phases=[PentestPhase.EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="offline_hash_cracking",
                description="Crack password hashes (LM, NTLM, MD5, SHA1) using precomputed rainbow tables; returns plaintext when a match is found; requires table files; not for salted hashes.",
                output_indicators=["hash", "cracked_password"],
            ),
        ],
        required_services=[],
        target_protocols=["file"],
        execution_priority=4,
        parallel_compatible=True,
        stealth_level=1,
        estimated_runtime_minutes=30,
    )
)
