"""Deprecated: cisco-torch is not a standard supported tool."""

from __future__ import annotations

import time
import warnings

from pydantic import Field

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult

warnings.warn(
    "cisco-torch is not a standard Kali tool and is deprecated.",
    DeprecationWarning,
    stacklevel=2,
)


class CiscoTorchArgs(BaseToolArgs):
    """Arguments for the deprecated cisco-torch tool."""

    scan_type: str | None = Field(
        default=None,
        description="Scan type (deprecated).",
    )


class CiscoTorchTool(BaseTool):
    """Deprecated cisco-torch tool placeholder."""

    args_model = CiscoTorchArgs

    def run(self, args: CiscoTorchArgs) -> ToolResult:
        _ = args
        start = time.time()
        return ToolResult(
            success=False,
            exit_code=-1,
            stdout="",
            stderr="cisco-torch is deprecated and not supported in this environment.",
            artifacts=[],
            metadata={"deprecated": True},
            execution_time=time.time() - start,
        )
