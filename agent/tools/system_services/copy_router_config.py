"""Deprecated: copy_router_config relies on expect scripting."""

from __future__ import annotations

import time
import warnings

from pydantic import Field

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult

warnings.warn(
    "copy_router_config relies on expect scripting and is deprecated.",
    DeprecationWarning,
    stacklevel=2,
)


class CopyRouterConfigArgs(BaseToolArgs):
    """Arguments for the deprecated copy_router_config tool."""

    vendor: str | None = Field(
        default=None,
        description="Router vendor (deprecated).",
    )


class CopyRouterConfigTool(BaseTool):
    """Deprecated copy_router_config tool placeholder."""

    args_model = CopyRouterConfigArgs

    def run(self, args: CopyRouterConfigArgs) -> ToolResult:
        _ = args
        start = time.time()
        return ToolResult(
            success=False,
            exit_code=-1,
            stdout="",
            stderr="copy_router_config is deprecated and not supported.",
            artifacts=[],
            metadata={"deprecated": True},
            execution_time=time.time() - start,
        )
