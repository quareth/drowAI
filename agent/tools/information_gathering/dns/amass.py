"""Execute OWASP Amass v5 and expose graph-free DNS discovery results."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator, model_validator
from runtime_shared.workspace_files import RuntimeWorkspaceDirectory, RuntimeWorkspaceFile

from ...base_tool import BaseTool
from ...canonical_capture import (
    CanonicalCaptureFormat,
    CaptureFamily,
    ToolCaptureContract,
)
from ...schemas import BaseToolArgs, ToolResult
from .amass_analysis import (
    AMASS_NAMES_BEGIN,
    AMASS_NAMES_END,
    AMASS_RESOLVED_BEGIN,
    AMASS_RESOLVED_END,
    normalize_dns_name,
    parse_amass_v5_results,
)
from .amass_semantics import (
    AMASS_CAPABILITY_FAMILY,
    AMASS_SEMANTIC_SCHEMA_VERSION,
    build_amass_evidence,
    build_amass_observations,
)

_COLLECTOR_RELATIVE_PATH = ".drowai/amass/collect_v5.sh"
_CONTAINER_COLLECTOR_PATH = f"/workspace/{_COLLECTOR_RELATIVE_PATH}"
_COLLECTOR_SCRIPT = f"""#!/usr/bin/env bash
set -u -o pipefail

if [ "$#" -lt 2 ]; then
    echo "usage: collect_v5.sh WORKSPACE_ROOT ROOT_DOMAIN [AMASS_ENUM_OPTIONS...]" >&2
    exit 2
fi

workspace_root=$1
root_domain=$2
shift 2

session_parent="${{workspace_root%/}}/.drowai/amass"
mkdir -p "$session_parent"
session_dir=$(mktemp -d "$session_parent/session.XXXXXX")
cleanup() {{
    rm -rf -- "$session_dir"
}}
trap cleanup EXIT

amass enum -dir "$session_dir" -d "$root_domain" "$@" 1>&2
enum_status=$?
if [ "$enum_status" -ne 0 ]; then
    exit "$enum_status"
fi

printf '%s\\n' '{AMASS_NAMES_BEGIN}'
amass subs -dir "$session_dir" -d "$root_domain" -names -nocolor
names_status=$?
printf '%s\\n' '{AMASS_NAMES_END}'
if [ "$names_status" -ne 0 ]; then
    exit "$names_status"
fi

printf '%s\\n' '{AMASS_RESOLVED_BEGIN}'
amass subs -dir "$session_dir" -d "$root_domain" -names -ip -nocolor
resolved_status=$?
printf '%s\\n' '{AMASS_RESOLVED_END}'
exit "$resolved_status"
"""


class Mode(str, Enum):
    """Supported Amass v5 domain-enumeration modes."""

    PASSIVE = "passive"
    ACTIVE = "active"
    BRUTE = "brute"


class AmassArgs(BaseToolArgs):
    """Validated graph-free Amass v5 domain-enumeration arguments."""

    mode: Mode = Field(
        Mode.PASSIVE,
        description="Scan mode to use",
    )
    wordlist: Optional[str] = Field(
        None,
        description="Custom wordlist for bruteforce",
    )
    inactivity_timeout_minutes: int = Field(
        30,
        ge=1,
        le=1440,
        description="Minutes without Amass progress before native termination",
    )
    execution_timeout: int = Field(
        600,
        ge=1,
        le=3600,
        description="Maximum wall-clock seconds to allow the Amass workflow to run",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output",
    )
    quiet: bool = Field(
        False,
        description="Suppress all output except for errors",
    )
    dns_server: Optional[str] = Field(
        None,
        description="DNS server to use for queries",
    )
    source: Optional[List[str]] = Field(
        None,
        description="Data sources to use",
    )
    exclude_source: Optional[List[str]] = Field(
        None,
        description="Data sources to exclude",
    )

    @field_validator("target")
    @classmethod
    def _validate_root_domain(cls, value: str) -> str:
        """Require one DNS root domain instead of an IP or mixed target list."""

        raw = str(value or "").strip()
        if "," in raw or any(character.isspace() for character in raw):
            raise ValueError("target must contain exactly one root domain")
        normalized = normalize_dns_name(raw)
        if normalized is None:
            raise ValueError("target must be a valid DNS root domain, not an IP address")
        return normalized

    @model_validator(mode="after")
    def _validate_output_controls(self) -> "AmassArgs":
        """Reject contradictory terminal-output controls."""

        if self.verbose and self.quiet:
            raise ValueError("verbose and quiet cannot both be enabled")
        return self


class AmassTool(BaseTool):
    """Run Amass v5 enumeration, query its session, and discard the graph."""

    args_model = AmassArgs
    _capture_contract = ToolCaptureContract(
        family=CaptureFamily.TEXT_NATIVE,
        canonical_format=CanonicalCaptureFormat.TEXT,
    )

    def build_command(self, args: AmassArgs) -> List[str]:
        """Build the task-runtime collector command."""

        return self._build_collector_command(
            args,
            workspace_root="/workspace",
            script_path=_CONTAINER_COLLECTOR_PATH,
        )

    def prepare_workspace_files(self, args: AmassArgs) -> List[RuntimeWorkspaceFile]:
        """Materialize the fixed Amass v5 collector in the task workspace."""

        _ = args
        return [
            RuntimeWorkspaceFile.from_text(
                relative_path=_COLLECTOR_RELATIVE_PATH,
                content=_COLLECTOR_SCRIPT,
                description="graph-free Amass v5 enum/subs collector",
            )
        ]

    def prepare_workspace_directories(
        self,
        args: AmassArgs,
    ) -> List[RuntimeWorkspaceDirectory]:
        """Create the parent used for short-lived Amass session directories."""

        _ = args
        return [
            RuntimeWorkspaceDirectory(
                relative_path=".drowai/amass",
                description="temporary Amass v5 sessions",
            )
        ]

    @staticmethod
    def _build_collector_command(
        args: AmassArgs,
        *,
        workspace_root: str,
        script_path: str,
    ) -> List[str]:
        """Return collector argv with native Amass enum options."""

        command = ["bash", script_path, workspace_root, args.target]
        enum_options: List[str] = []

        if args.mode == Mode.ACTIVE:
            enum_options.append("-active")
        elif args.mode == Mode.BRUTE:
            enum_options.append("-brute")

        if args.wordlist:
            if "-brute" not in enum_options:
                enum_options.append("-brute")
            enum_options.extend(["-w", args.wordlist])

        enum_options.extend(["-timeout", str(args.inactivity_timeout_minutes)])

        if args.verbose:
            enum_options.append("-v")
        if args.quiet:
            enum_options.append("-silent")
        if args.dns_server:
            enum_options.extend(["-r", args.dns_server])
        if args.source:
            enum_options.extend(["-include", ",".join(args.source)])
        if args.exclude_source:
            enum_options.extend(["-exclude", ",".join(args.exclude_source)])

        enum_options.append("-nocolor")
        command.extend(enum_options)
        return command

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: AmassArgs,
    ) -> Dict[str, Any]:
        """Parse the collector's tagged ``amass subs`` result stream."""

        _ = stderr, args
        metadata = parse_amass_v5_results(stdout, exit_code=exit_code)
        metadata["semantic_schema_version"] = AMASS_SEMANTIC_SCHEMA_VERSION
        metadata["capability_family"] = AMASS_CAPABILITY_FAMILY
        return metadata

    def emit_semantic_observations(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: AmassArgs,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Emit DNS, IP, and resolves_to observations from normalized metadata."""

        _ = stdout, stderr, exit_code, args
        return build_amass_observations(metadata)

    def emit_semantic_evidence(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: AmassArgs,
        metadata: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Emit bounded Amass run evidence from normalized metadata."""

        _ = stdout, stderr, exit_code
        return build_amass_evidence(metadata, args)

    def create_artifacts(
        self,
        stdout: str,
        args: AmassArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create amass artifact files from output.
        
        Args:
            stdout: Command stdout
            args: Original AmassArgs
            timestamp: Optional timestamp for artifact naming
            
        Returns:
            List of artifact file paths created
        """
        artifacts: List[str] = []
        
        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = f"artifacts/amass_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass  # Artifact creation is optional
        
        return artifacts

    def run(self, args: AmassArgs) -> ToolResult:
        """Execute the same collector contract outside container transports."""

        temporary_workspace = tempfile.TemporaryDirectory(prefix="drowai-amass-")
        workspace_root = Path(temporary_workspace.name)
        script_path = workspace_root / _COLLECTOR_RELATIVE_PATH
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(_COLLECTOR_SCRIPT, encoding="utf-8")
        cmd = self._build_collector_command(
            args,
            workspace_root=str(workspace_root),
            script_path=str(script_path),
        )

        start = time.time()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=args.execution_timeout,
            )
        except subprocess.TimeoutExpired:
            temporary_workspace.cleanup()
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
        temporary_workspace.cleanup()

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
from ...enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="information_gathering.dns.amass",
        display_name="Amass",
        category=ToolCategory.DNS_ENUMERATION,
        applicable_phases=[PentestPhase.RECONNAISSANCE, PentestPhase.ENUMERATION],
        capabilities=[
            ToolCapability(
                name="subdomain_enumeration",
                description="Enumerate subdomains for a domain via passive intel and active DNS bruteforce; returns discovered hostnames; use for thorough subdomain coverage",
                output_indicators=["Found", "Subdomain"],
            ),
        ],
        required_services=["dns"],
        target_protocols=["udp"],
        execution_priority=7,
        parallel_compatible=False,
        stealth_level=3,
        estimated_runtime_minutes=15,
    )
)
