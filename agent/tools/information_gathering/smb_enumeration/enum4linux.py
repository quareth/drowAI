"""Enum4Linux tool for SMB enumeration."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class Enum4LinuxMode(str, Enum):
    """Supported Enum4Linux modes."""

    BASIC = "basic"
    FULL = "full"
    USERS = "users"
    SHARES = "shares"
    GROUPS = "groups"
    PASSWORDS = "passwords"


class Enum4LinuxArgs(BaseToolArgs):
    """Arguments for the Enum4Linux tool."""

    mode: Enum4LinuxMode = Field(
        Enum4LinuxMode.BASIC,
        description="Enum4Linux mode to use",
    )
    username: Optional[str] = Field(
        None,
        description="Username for authentication",
    )
    password: Optional[str] = Field(
        None,
        description="Password for authentication",
    )
    domain: Optional[str] = Field(
        None,
        description="Domain name",
    )
    workgroup: Optional[str] = Field(
        None,
        description="Workgroup name",
    )
    port: int = Field(
        445,
        description="SMB port",
    )
    timeout: int = Field(
        30,
        description="Connection timeout in seconds",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output for detailed information",
    )
    output_file: Optional[str] = Field(
        None,
        description="Output file for results",
    )
    max_timeout: int = Field(
        300,
        description="Maximum execution time in seconds before the tool is terminated",
    )


def parse_enum4linux_output(output_text: str) -> Dict[str, Any]:
    """Parse Enum4Linux output into structured metadata."""
    metadata: Dict[str, Any] = {
        "users_found": [],
        "shares_found": [],
        "groups_found": [],
        "passwords_found": [],
        "domains_found": [],
        "workgroups_found": [],
        "total_users": 0,
        "total_shares": 0,
        "total_groups": 0,
        "scan_completed": False,
        "errors": [],
    }

    lines = output_text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Parse users found
        if "user:" in line.lower() or "username:" in line.lower():
            try:
                import re
                match = re.search(r'[A-Za-z0-9_]+', line)
                if match:
                    user = match.group(0)
                    metadata["users_found"].append(user)
                    metadata["total_users"] += 1
            except Exception:
                pass
        # Parse shares found
        elif "share:" in line.lower() or "disk:" in line.lower():
            try:
                import re
                match = re.search(r'[A-Za-z0-9_]+', line)
                if match:
                    share = match.group(0)
                    metadata["shares_found"].append(share)
                    metadata["total_shares"] += 1
            except Exception:
                pass
        # Parse groups found
        elif "group:" in line.lower():
            try:
                import re
                match = re.search(r'[A-Za-z0-9_]+', line)
                if match:
                    group = match.group(0)
                    metadata["groups_found"].append(group)
                    metadata["total_groups"] += 1
            except Exception:
                pass
        # Parse passwords found
        elif "password:" in line.lower():
            try:
                import re
                match = re.search(r'[A-Za-z0-9_!@#$%^&*]+', line)
                if match:
                    password = match.group(0)
                    metadata["passwords_found"].append(password)
            except Exception:
                pass
        # Parse domains found
        elif "domain:" in line.lower():
            try:
                import re
                match = re.search(r'[A-Za-z0-9_.]+', line)
                if match:
                    domain = match.group(0)
                    metadata["domains_found"].append(domain)
            except Exception:
                pass
        # Parse workgroups found
        elif "workgroup:" in line.lower():
            try:
                import re
                match = re.search(r'[A-Za-z0-9_]+', line)
                if match:
                    workgroup = match.group(0)
                    metadata["workgroups_found"].append(workgroup)
            except Exception:
                pass
        # Parse scan completion
        elif "scan" in line.lower() and "completed" in line.lower():
            metadata["scan_completed"] = True
        # Parse errors
        elif "error" in line.lower() or "failed" in line.lower():
            metadata["errors"].append(line)

    return metadata


class Enum4LinuxTool(BaseTool):
    """Enum4Linux tool for SMB enumeration."""

    args_model = Enum4LinuxArgs

    def run(self, args: Enum4LinuxArgs) -> ToolResult:
        # Build command array
        cmd = ["enum4linux"]

        # Add mode
        cmd.extend(["--mode", args.mode.value])

        # Add username if provided
        if args.username:
            cmd.extend(["-u", args.username])

        # Add password if provided
        if args.password:
            cmd.extend(["-p", args.password])

        # Add domain if provided
        if args.domain:
            cmd.extend(["-d", args.domain])

        # Add workgroup if provided
        if args.workgroup:
            cmd.extend(["-w", args.workgroup])

        # Add port
        cmd.extend(["-P", str(args.port)])

        # Add timeout
        cmd.extend(["-t", str(args.timeout)])

        # Add verbose flag
        if args.verbose:
            cmd.append("-v")

        # Add output file if provided
        if args.output_file:
            cmd.extend(["-o", args.output_file])

        # Add target (usually last)
        cmd.append(args.target)

        # Execute with timing
        start = time.time()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.max_timeout,
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

        # Parse output for metadata
        metadata = parse_enum4linux_output(proc.stdout)

        # Generate artifacts if needed
        artifacts: List[str] = []
        if proc.stdout and len(proc.stdout) > 100:  # If significant output
            timestamp = int(start)
            artifact_path = f"artifacts/enum4linux_{timestamp}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(proc.stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass  # Artifact creation is optional

        return ToolResult(
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start,
        )
