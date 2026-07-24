"""Enum4linux-ng command adapter and output parser for SMB enumeration."""

from __future__ import annotations

import os
import subprocess
import time
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field, model_validator

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
    """Arguments supported by the enum4linux-ng command adapter."""

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
        description="Deprecated alias for workgroup",
        json_schema_extra={"deprecated": True},
    )
    workgroup: Optional[str] = Field(
        None,
        description="Workgroup name",
    )
    port: int = Field(
        445,
        ge=445,
        le=445,
        description="SMB port; enum4linux-ng currently supports its default port only",
    )
    timeout: int = Field(
        30,
        ge=1,
        le=600,
        description="Connection timeout in seconds",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output for detailed information",
    )
    output_file: Optional[str] = Field(
        None,
        description="Output basename for enum4linux-ng JSON and YAML results",
    )
    max_timeout: int = Field(
        300,
        ge=1,
        le=3600,
        description="Maximum execution time in seconds before the tool is terminated",
    )

    @model_validator(mode="after")
    def validate_workgroup_aliases(self) -> "Enum4LinuxArgs":
        """Reject ambiguous values for enum4linux-ng's single -w option."""
        if self.domain and self.workgroup and self.domain != self.workgroup:
            raise ValueError("domain and workgroup must match when both are provided")
        return self


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
    """Execute enum4linux-ng for SMB enumeration."""

    args_model = Enum4LinuxArgs

    _MODE_FLAGS = {
        Enum4LinuxMode.BASIC: ["-A"],
        Enum4LinuxMode.FULL: ["-A", "-C", "-R"],
        Enum4LinuxMode.USERS: ["-U"],
        Enum4LinuxMode.SHARES: ["-S"],
        Enum4LinuxMode.GROUPS: ["-G"],
        Enum4LinuxMode.PASSWORDS: ["-P"],
    }

    def build_command(self, args: Enum4LinuxArgs) -> List[str]:
        """Build an enum4linux-ng command using only supported CLI flags."""
        cmd = ["enum4linux-ng", *self._MODE_FLAGS[args.mode]]

        if args.username:
            cmd.extend(["-u", args.username])

        if args.password:
            cmd.extend(["-p", args.password])

        workgroup = args.workgroup or args.domain
        if workgroup:
            cmd.extend(["-w", workgroup])

        cmd.extend(["-t", str(args.timeout)])

        if args.verbose:
            cmd.append("-v")

        if args.output_file:
            cmd.extend(["-oA", args.output_file])

        cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: Enum4LinuxArgs,
    ) -> Dict[str, Any]:
        """Parse enum4linux-ng text output into structured metadata."""
        _ = stderr, exit_code, args
        return parse_enum4linux_output(stdout)

    def create_artifacts(
        self,
        stdout: str,
        args: Enum4LinuxArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Persist significant text output as an optional artifact."""
        _ = args
        artifacts: List[str] = []
        if not stdout or len(stdout) <= 100:
            return artifacts

        artifact_timestamp = timestamp if timestamp is not None else int(time.time())
        artifact_path = f"artifacts/enum4linux_{artifact_timestamp}.txt"
        try:
            os.makedirs("artifacts", exist_ok=True)
            with open(artifact_path, "w", encoding="utf-8") as artifact_file:
                artifact_file.write(stdout)
            artifacts.append(artifact_path)
        except Exception:
            pass
        return artifacts

    def run(self, args: Enum4LinuxArgs) -> ToolResult:
        """Execute enum4linux-ng and normalize its result."""
        cmd = self.build_command(args)

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

        metadata = self.parse_output(proc.stdout, proc.stderr, proc.returncode, args)
        artifacts = self.create_artifacts(proc.stdout, args, timestamp=int(start))

        return ToolResult(
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start,
        )
