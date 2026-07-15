from __future__ import annotations

"""Enhanced tool metadata models for intelligent selection."""

from enum import Enum  # noqa: E402
from typing import List, Optional  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402

from .categories import PentestPhase, ToolCategory  # noqa: E402


class ToolCatalogRole(str, Enum):
    """Describes how a tool participates in user-facing catalog configuration."""

    PENTEST = "pentest"
    UTILITY = "utility"
    SYSTEM = "system"


class ToolCapability(BaseModel):
    """Describes a discrete capability provided by a tool."""

    name: str
    description: str
    output_indicators: List[str] = Field(default_factory=list)


class EnhancedToolMetadata(BaseModel):
    """Metadata describing tool capabilities and usage characteristics."""

    tool_id: str = Field(..., description="Full dotted path tool identifier")
    display_name: str = Field(..., description="Human-readable tool name")
    category: ToolCategory
    catalog_role: ToolCatalogRole = Field(
        default=ToolCatalogRole.PENTEST,
        description="Configuration role for user-facing catalog policy",
    )
    applicable_phases: List[PentestPhase] = Field(default_factory=list)
    capabilities: List[ToolCapability] = Field(default_factory=list)
    required_services: List[str] = Field(
        default_factory=list, description='Services required, e.g., ["http", "https"]'
    )
    target_protocols: List[str] = Field(
        default_factory=list, description='Protocols targeted, e.g., ["tcp", "udp"]'
    )
    execution_priority: int = Field(
        default=5, ge=1, le=10, description="1-10 priority (higher executes earlier)"
    )
    parallel_compatible: bool = Field(
        default=True, description="Whether the tool can run concurrently with others"
    )
    batch_audited: bool = Field(
        default=False,
        description=(
            "Deprecated rollout marker retained for metadata compatibility. "
            "Runtime batch admission is governed by parallel_compatible, "
            "avoid_with, and concurrency limits."
        ),
    )
    stealth_level: int = Field(
        default=3, ge=1, le=5, description="1-5 stealthiness rating"
    )
    best_combined_with: List[str] = Field(default_factory=list)
    avoid_with: List[str] = Field(default_factory=list)
    prerequisite_tools: List[str] = Field(default_factory=list)
    max_concurrent_per_target: int = Field(default=1, ge=1)
    estimated_runtime_minutes: int = Field(default=5, ge=1)
    memory_requirement_mb: int = Field(default=100, ge=1)
    supported_transports: Optional[List[str]] = Field(
        None,
        description=(
            "Execution transports supported by this tool. "
            "Container-scoped tools use 'file-comm' or 'pty'. "
            "'direct' is only for backend-scoped or artifact-scoped tools."
        ),
    )
