"""Shared Pydantic argument/result schemas for agent tool contracts."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

ContainerTransport = Literal["file-comm", "pty"]
"""Execution transport types for tools that must run inside the Kali runtime."""

CONTAINER_TRANSPORT_DESCRIPTION = (
    "Execution transport preference for Kali runtime tools: file-comm or pty. "
    "Auto-selected if not specified."
)


class OutputFormat(str, Enum):
    """Common output format options used by tool argument schemas."""

    TEXT = "text"
    JSON = "json"
    XML = "xml"
    CSV = "csv"
    RAW = "raw"


class BaseToolArgs(BaseModel):
    """Common arguments for all tools.
    
    Only 'target' is required. All other fields are optional.
    Defaults are handled in tool implementations, not in schema.
    """

    target: str = Field(
        ..., description="Target IP address or hostname"
    )
    timeout: Optional[int] = Field(
        None, description="Execution timeout in seconds (tool default if omitted)"
    )

    def model_post_init(self, __context: Any) -> None:
        """Apply runtime defaults without embedding them into the JSON schema.

        Many tools assume `timeout` is an int. We keep it optional in schema to
        reduce over-specification, but fill a sensible runtime default when
        omitted.
        """
        if self.timeout is None:
            self.timeout = 30


class ToolResult(BaseModel):
    """Standard result returned by tools."""

    success: bool = Field(
        ..., description="Whether the tool executed successfully without critical errors"
    )
    exit_code: int = Field(
        ..., description="Process exit code (0 for success, non-zero for failure)"
    )
    stdout: str = Field(..., description="Standard output from the tool execution")
    stderr: str = Field(
        ..., description="Standard error output, including warnings and error messages"
    )
    artifacts: List[str] = Field(
        default_factory=list,
        description="List of file paths to generated artifacts (XML reports, logs, etc.)",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Tool-specific metadata and parsed results",
    )
    execution_time: float = Field(
        ..., description="Actual execution time in seconds"
    )
    validation_errors: Optional[List[Dict[str, str]]] = Field(
        None, description="Schema validation errors for LLM feedback"
    )
