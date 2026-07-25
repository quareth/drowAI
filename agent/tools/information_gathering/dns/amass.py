"""Execute OWASP Amass v5 and expose graph-free DNS discovery results."""

from __future__ import annotations

import os
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
    normalize_dns_name,
    parse_amass_v5_results,
)
from .amass_runtime import (
    AMASS_CONTAINER_COLLECTOR_PATH,
    build_amass_collector_command,
    execute_amass_collector_locally,
    prepare_amass_workspace_directories,
    prepare_amass_workspace_files,
)
from .amass_semantics import (
    AMASS_CAPABILITY_FAMILY,
    AMASS_SEMANTIC_SCHEMA_VERSION,
    build_amass_evidence,
    build_amass_observations,
)


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

        return build_amass_collector_command(
            args,
            workspace_root="/workspace",
            script_path=AMASS_CONTAINER_COLLECTOR_PATH,
        )

    def prepare_workspace_files(self, args: AmassArgs) -> List[RuntimeWorkspaceFile]:
        """Materialize the fixed Amass v5 collector in the task workspace."""

        return prepare_amass_workspace_files(args)

    def prepare_workspace_directories(
        self,
        args: AmassArgs,
    ) -> List[RuntimeWorkspaceDirectory]:
        """Create task-scoped Amass runtime state directories."""

        return prepare_amass_workspace_directories(args)

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: AmassArgs,
    ) -> Dict[str, Any]:
        """Parse the collector's tagged ``amass subs`` result stream."""

        _ = stderr, args
        metadata = parse_amass_v5_results(
            stdout,
            exit_code=exit_code,
            root_domain=args.target,
        )
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

        start = time.time()
        execution = execute_amass_collector_locally(args)
        metadata = self.parse_output(
            execution.stdout,
            execution.stderr,
            execution.exit_code,
            args,
        )
        artifacts = self.create_artifacts(execution.stdout, args, timestamp=int(start))

        return ToolResult(
            success=execution.exit_code == 0,
            exit_code=execution.exit_code,
            stdout=execution.stdout,
            stderr=execution.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=execution.execution_time,
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
