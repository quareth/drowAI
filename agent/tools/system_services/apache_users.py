"""Deprecated: apache-users is not a standard supported tool."""

from __future__ import annotations

import time
import warnings

from pydantic import Field

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult

warnings.warn(
    "apache-users is not a standard Kali tool and is deprecated.",
    DeprecationWarning,
    stacklevel=2,
)


class ApacheUsersArgs(BaseToolArgs):
    """Arguments for the deprecated apache-users tool."""

    wordlist: str | None = Field(
        default=None,
        description="Username wordlist (deprecated).",
    )


class ApacheUsersTool(BaseTool):
    """Deprecated apache-users tool placeholder."""

    args_model = ApacheUsersArgs

    def run(self, args: ApacheUsersArgs) -> ToolResult:
        _ = args
        start = time.time()
        return ToolResult(
            success=False,
            exit_code=-1,
            stdout="",
            stderr="apache-users is deprecated and not supported in this environment.",
            artifacts=[],
            metadata={"deprecated": True},
            execution_time=time.time() - start,
        )
