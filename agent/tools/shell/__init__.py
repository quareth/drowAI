"""Shell execution tools with workspace safeguards."""

from __future__ import annotations

from .exec import ShellExecTool
from .policy import CommandPolicy, PolicyEnforcement, PolicyResult
from .script import ShellScriptTool
from ..enhanced_metadata_registry import (
    EnhancedToolMetadata,
    PentestPhase,
    ToolCapability,
    ToolCatalogRole,
    ToolCategory,
    register_enhanced_tool_metadata,
)

__all__ = [
    "ShellExecTool",
    "ShellScriptTool",
    "CommandPolicy",
    "PolicyEnforcement",
    "PolicyResult",
]

# Enhanced metadata with PTY details
_shell_exec_metadata = EnhancedToolMetadata(
    tool_id="shell.exec",
    display_name="Shell Command Executor",
    category=ToolCategory.SHELL,
    catalog_role=ToolCatalogRole.UTILITY,
    applicable_phases=[
        PentestPhase.RECONNAISSANCE,
        PentestPhase.ENUMERATION,
        PentestPhase.POST_EXPLOITATION,
    ],
    capabilities=[
        ToolCapability(
            name="shell_command",
            description="Execute one guarded shell command inside the active Kali runtime; returns stdout, stderr, exit code, and artifacts.",
            output_indicators=["stdout", "stderr"],
        )
    ],
    required_services=[],
    target_protocols=["local"],
    execution_priority=4,
    parallel_compatible=True,
    stealth_level=3,
    estimated_runtime_minutes=2,
    supported_transports=["file-comm", "pty"],
)
_shell_exec_metadata.__dict__["pty_support"] = True
_shell_exec_metadata.__dict__["pty_benefits"] = ["visibility", "debugging", "user_interaction"]

_shell_script_metadata = EnhancedToolMetadata(
    tool_id="shell.script",
    display_name="Workspace Script Runner",
    category=ToolCategory.SHELL,
    catalog_role=ToolCatalogRole.UTILITY,
    applicable_phases=[
        PentestPhase.RECONNAISSANCE,
        PentestPhase.ENUMERATION,
        PentestPhase.POST_EXPLOITATION,
    ],
    capabilities=[
        ToolCapability(
            name="shell_script",
            description="Execute a guarded multi-line shell script inside the active Kali runtime; returns stdout, stderr, and exit code; use when one command is not enough.",
            output_indicators=["stdout", "stderr"],
        )
    ],
    required_services=[],
    target_protocols=["local"],
    execution_priority=4,
    parallel_compatible=True,
    stealth_level=3,
    estimated_runtime_minutes=3,
    supported_transports=["file-comm", "pty"],
)
_shell_script_metadata.__dict__["pty_support"] = True
_shell_script_metadata.__dict__["pty_benefits"] = ["script_debugging", "output_visibility", "error_tracking"]

register_enhanced_tool_metadata(_shell_exec_metadata)
register_enhanced_tool_metadata(_shell_script_metadata)
