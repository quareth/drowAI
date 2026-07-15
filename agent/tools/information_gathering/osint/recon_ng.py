"""Recon-ng OSINT information gathering tool using Pydantic models."""

from __future__ import annotations

import os
import subprocess
import time
import json
from enum import Enum
from typing import List, Optional, Dict, Any

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


class ModuleType(str, Enum):
    """Recon-ng module types."""
    
    RECON = "recon"
    EXPLOITATION = "exploitation"
    REPORTING = "reporting"
    DISCOVERY = "discovery"
    IMPORT = "import"
    EXPORT = "export"


class OutputFormat(str, Enum):
    """Recon-ng output format options."""
    
    JSON = "json"
    CSV = "csv"
    XML = "xml"
    TEXT = "text"


class ReconNgArgs(BaseToolArgs):
    """Arguments for the Recon-ng tool."""

    module_type: ModuleType = Field(
        ModuleType.RECON,
        description="Type of Recon-ng module to use",
    )
    module_name: str = Field(
        "hosts-hosts/resolve",
        description="Specific module to execute",
    )
    output_format: OutputFormat = Field(
        OutputFormat.JSON,
        description="Output format for parsing - JSON recommended for structured data",
    )
    workspace: Optional[str] = Field(
        None,
        description="Workspace to use for the operation",
    )
    api_key: Optional[str] = Field(
        None,
        description="API key for external services",
    )
    max_results: int = Field(
        100,
        ge=1,
        le=1000,
        description="Maximum number of results to return",
    )
    timeout: int = Field(
        60,
        ge=10,
        le=600,
        description="Timeout in seconds for module execution",
    )
    verbose: bool = Field(
        False,
        description="Enable verbose output",
    )
    debug: bool = Field(
        False,
        description="Enable debug mode",
    )
    no_banner: bool = Field(
        True,
        description="Suppress banner output",
    )


def parse_recon_ng_json(json_text: str) -> Dict[str, Any]:
    """Parse Recon-ng JSON output into structured metadata."""
    
    metadata: Dict[str, Any] = {"results": [], "summary": {}}
    
    try:
        data = json.loads(json_text)
        
        # Handle different response types
        if isinstance(data, list):
            metadata["results"] = data
        elif isinstance(data, dict):
            if "results" in data:
                metadata["results"] = data["results"]
            elif "data" in data:
                metadata["results"] = data["data"]
            else:
                metadata["results"] = [data]
            
            # Extract summary information
            if "total" in data:
                metadata["summary"]["total"] = data["total"]
            if "module" in data:
                metadata["summary"]["module"] = data["module"]
            if "status" in data:
                metadata["summary"]["status"] = data["status"]
        
        # Generate summary if not provided
        if not metadata["summary"]:
            metadata["summary"] = {
                "total_results": len(metadata["results"]),
                "result_types": list(set(type(r).__name__ for r in metadata["results"]))
            }
        
    except json.JSONDecodeError as e:
        metadata["error"] = f"Failed to parse JSON: {str(e)}"
    
    return metadata


def parse_recon_ng_text(text_output: str) -> Dict[str, Any]:
    """Parse Recon-ng text output into structured metadata."""
    
    metadata: Dict[str, Any] = {
        "results": [],
        "summary": {},
        "modules": [],
        "workspaces": []
    }
    
    try:
        lines = text_output.strip().split('\n')
        current_section = None
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            # Detect sections
            if "Results:" in line:
                current_section = "results"
            elif "Modules:" in line:
                current_section = "modules"
            elif "Workspaces:" in line:
                current_section = "workspaces"
            elif "Summary:" in line:
                current_section = "summary"
            
            # Parse content based on current section
            elif current_section == "results":
                if line and not line.startswith("Results:"):
                    metadata["results"].append(line)
            
            elif current_section == "modules":
                if line and not line.startswith("Modules:"):
                    metadata["modules"].append(line)
            
            elif current_section == "workspaces":
                if line and not line.startswith("Workspaces:"):
                    metadata["workspaces"].append(line)
            
            elif current_section == "summary":
                if ":" in line and not line.startswith("Summary:"):
                    key, value = line.split(":", 1)
                    metadata["summary"][key.strip()] = value.strip()
        
        # Clean up empty sections
        metadata = {k: v for k, v in metadata.items() if v}
        
    except Exception as e:
        metadata["error"] = f"Failed to parse text output: {str(e)}"
    
    return metadata


class ReconNgTool(BaseTool):
    """Run Recon-ng modules and parse the results."""

    args_model = ReconNgArgs

    def build_command(self, args: ReconNgArgs) -> List[str]:
        """Build recon-ng command arguments.
        
        Args:
            args: Validated ReconNgArgs
            
        Returns:
            List of command arguments for recon-ng
        """
        cmd = ["recon-ng"]
        
        # Add no banner/check flags for CLI mode
        cmd.extend(["--no-version-check"])
        
        # Add workspace if specified
        if args.workspace:
            cmd.extend(["-w", args.workspace])
        
        # Build commands to run inside recon-ng
        # recon-ng uses -c for commands
        commands = []
        
        # Load module
        module_path = f"{args.module_type.value}/{args.module_name}"
        commands.append(f"modules load {module_path}")
        
        # Set target as source
        commands.append(f"options set SOURCE {args.target}")
        
        # Run the module
        commands.append("run")
        
        # Exit
        commands.append("exit")
        
        # Add all commands with -c flags
        for command in commands:
            cmd.extend(["-c", command])
        
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: ReconNgArgs,
    ) -> Dict[str, Any]:
        """Parse recon-ng output into structured metadata."""
        if stdout:
            return parse_recon_ng_text(stdout)
        return {}

    def create_artifacts(
        self,
        stdout: str,
        args: ReconNgArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        """Create recon-ng artifact files from output."""
        artifacts: List[str] = []
        if stdout:
            ts = timestamp if timestamp is not None else int(time.time())
            safe_name = args.module_name.replace('/', '_').replace('-', '_')
            artifact_path = f"artifacts/recon_ng_{safe_name}_{ts}.txt"
            try:
                os.makedirs("artifacts", exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as f:
                    f.write(stdout)
                artifacts.append(artifact_path)
            except Exception:
                pass
        return artifacts

    def run(self, args: ReconNgArgs) -> ToolResult:
        cmd = self.build_command(args)

        start = time.time()
        try:
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


from ...enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)


register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="information_gathering.osint.recon_ng",
        display_name="Recon-ng",
        category=ToolCategory.WEB_ENUMERATION,
        applicable_phases=[
            PentestPhase.RECONNAISSANCE,
            PentestPhase.ENUMERATION,
        ],
        capabilities=[
            ToolCapability(
                name="module_execution",
                description="Run Recon-ng modules to gather domain, host, contact, or credential intel; returns module records; use only when a specific module is required",
                output_indicators=["module", "host", "contact"],
            ),
        ],
        required_services=[],
        target_protocols=["http", "https", "dns"],
        execution_priority=5,
        parallel_compatible=False,
        stealth_level=4,
        estimated_runtime_minutes=10,
    )
)
