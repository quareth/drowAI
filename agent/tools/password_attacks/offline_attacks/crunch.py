"""
Crunch - Wordlist generator.

Implements execution-model hooks for PTY/file-comm compatibility.
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class CrunchArgs(BaseToolArgs):
    """Arguments for the Crunch tool."""

    min_length: int = Field(..., description="Minimum length of generated passwords", ge=1, le=50)
    max_length: int = Field(..., description="Maximum length of generated passwords", ge=1, le=50)
    charset: Optional[str] = Field(
        None,
        description="Character set to use (e.g., abc123). Defaults to lowercase letters if omitted.",
    )
    pattern: Optional[str] = Field(
        None,
        description="Pattern using placeholders (e.g., @@dog%%) to constrain positions.",
    )
    start: Optional[str] = Field(None, description="Start string for generation range")
    end: Optional[str] = Field(None, description="End string for generation range")
    lines_per_file: Optional[int] = Field(
        None,
        description="Number of lines per output file (chunks large wordlists)",
        gt=0,
    )
    output_file: Optional[str] = Field(
        None,
        description="Destination file path for generated wordlist. Defaults to artifacts/crunch_<ts>.txt",
    )

    @field_validator("max_length")
    @classmethod
    def validate_lengths(cls, max_length: int, info) -> int:
        min_length = info.data.get("min_length")
        if min_length is not None and max_length < min_length:
            raise ValueError("max_length must be >= min_length")
        return max_length


def parse_crunch_output(stdout: str) -> Dict[str, Any]:
    """Extract high-level stats from Crunch output."""
    metadata: Dict[str, Any] = {
        "lines_generated": 0,
        "execution_status": "unknown",
    }

    try:
        for line in stdout.splitlines():
            if "Crunch will now generate" in line:
                metadata["execution_status"] = "started"
            if "Generating output" in line:
                metadata["execution_status"] = "running"
            if "Done" in line:
                metadata["execution_status"] = "completed"
            if "Generating list length" in line:
                parts = line.split()
                for part in parts:
                    if part.isdigit():
                        metadata["lines_generated"] = int(part)
                        break
    except Exception:
        metadata["execution_status"] = "parsing_error"

    return metadata


class CrunchTool(BaseTool):
    """Generate wordlists using Crunch."""

    args_model = CrunchArgs

    def build_command(self, args: CrunchArgs) -> List[str]:
        """Build crunch command arguments.
        
        Args:
            args: Validated CrunchArgs
            
        Returns:
            List of command arguments for crunch
        """
        cmd: List[str] = ["crunch", str(args.min_length), str(args.max_length)]

        charset = args.charset or "abcdefghijklmnopqrstuvwxyz"
        cmd.append(charset)

        if args.pattern:
            cmd.extend(["-t", args.pattern])
        if args.start:
            cmd.extend(["-s", args.start])
        if args.end:
            cmd.extend(["-e", args.end])
        if args.lines_per_file:
            cmd.extend(["-c", str(args.lines_per_file)])

        # Generate output path from args or default
        output_path = args.output_file or f"artifacts/crunch_{int(time.time())}.txt"
        cmd.extend(["-o", output_path])
        
        return cmd

    def _build_command_with_output(self, args: CrunchArgs, output_path: str) -> List[str]:
        """Internal method to build command with specific output path."""
        cmd: List[str] = ["crunch", str(args.min_length), str(args.max_length)]

        charset = args.charset or "abcdefghijklmnopqrstuvwxyz"
        cmd.append(charset)

        if args.pattern:
            cmd.extend(["-t", args.pattern])
        if args.start:
            cmd.extend(["-s", args.start])
        if args.end:
            cmd.extend(["-e", args.end])
        if args.lines_per_file:
            cmd.extend(["-c", str(args.lines_per_file)])

        cmd.extend(["-o", output_path])
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: CrunchArgs,
    ) -> Dict[str, Any]:
        """Parse crunch output into structured metadata."""
        metadata = parse_crunch_output(stdout or "")
        metadata.update(
            {
                "exit_code": exit_code,
                "pattern": args.pattern,
                "charset": args.charset or "lowercase",
                "min_length": args.min_length,
                "max_length": args.max_length,
            }
        )
        if stderr:
            metadata["stderr"] = stderr
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: CrunchArgs,
        timestamp: Optional[int] = None,
        output_path: Optional[str] = None,
    ) -> List[str]:
        """Create crunch artifact files from output."""
        artifacts: List[str] = []
        if output_path and os.path.exists(output_path):
            artifacts.append(output_path)

        if stdout and len(stdout) > 200:
            ts = int(timestamp or time.time())
            try:
                os.makedirs("artifacts", exist_ok=True)
                artifact_path = f"artifacts/crunch_stdout_{ts}.txt"
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except OSError:
                pass
        return artifacts

    def run(self, args: CrunchArgs) -> ToolResult:
        start = time.time()
        output_path = args.output_file or f"artifacts/crunch_{int(start)}.txt"
        try:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
        except OSError:
            # Fall back to current directory if artifacts cannot be created
            output_path = args.output_file or f"crunch_{int(start)}.txt"

        try:
            cmd = self._build_command_with_output(args, output_path=output_path)
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=900,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                exit_code=-2,
                stdout="",
                stderr="crunch command timed out",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        except FileNotFoundError:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr="crunch command not found. Please ensure Crunch is installed.",
                artifacts=[],
                metadata={},
                execution_time=time.time() - start,
            )
        except Exception as exc:
            msg = f"Error executing crunch: {exc}"
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
        metadata["output_file"] = output_path
        artifacts = self.create_artifacts(proc.stdout, args, timestamp=int(start), output_path=output_path)

        return ToolResult(
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start,
        )


__all__ = ["CrunchTool", "CrunchArgs"]


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
        tool_id="password_attacks.offline_attacks.crunch",
        display_name="Crunch",
        category=ToolCategory.PASSWORD_ATTACKS,
        applicable_phases=[PentestPhase.EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="wordlist_generation",
                description="Generate custom wordlists from charset, length range, and pattern rules; returns wordlist file path; use for feeding other crackers, not for cracking.",
                output_indicators=["wordlist", "password_candidate"],
            ),
        ],
        required_services=[],
        target_protocols=["file"],
        execution_priority=3,
        parallel_compatible=True,
        stealth_level=1,
        estimated_runtime_minutes=10,
    )
)