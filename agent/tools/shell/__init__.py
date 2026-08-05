"""Shell execution tools and shell metadata registration authority."""

from __future__ import annotations

from .exec import ShellExecTool
from .policy import CommandPolicy, PolicyEnforcement, PolicyResult
from .script import ShellScriptTool
from .write_stdin import ShellWriteStdinTool
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
    "ShellWriteStdinTool",
    "CommandPolicy",
    "PolicyEnforcement",
    "PolicyResult",
]

# Enhanced metadata with PTY-only session details
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
            description=(
                "Start one guarded provider-backed PTY shell session inside the "
                "active task runtime; returns bounded output, process status, "
                "and an opaque continuation handle when the process is still running."
            ),
            output_indicators=[
                "stdout",
                "stderr",
                "process_status",
                "session_id",
            ],
        )
    ],
    required_services=[],
    target_protocols=["local"],
    execution_priority=4,
    parallel_compatible=True,
    stealth_level=3,
    estimated_runtime_minutes=2,
    supported_transports=["pty"],
)
_shell_exec_metadata.__dict__["pty_support"] = True
_shell_exec_metadata.__dict__["pty_session_only"] = True
_shell_exec_metadata.__dict__["pty_benefits"] = [
    "yielded_sessions",
    "bounded_output",
    "user_interaction",
]

_shell_write_stdin_metadata = EnhancedToolMetadata(
    tool_id="shell.write_stdin",
    display_name="Shell Session Input",
    category=ToolCategory.SHELL,
    catalog_role=ToolCatalogRole.UTILITY,
    applicable_phases=[
        PentestPhase.RECONNAISSANCE,
        PentestPhase.ENUMERATION,
        PentestPhase.POST_EXPLOITATION,
    ],
    capabilities=[
        ToolCapability(
            name="shell_stdin",
            description=(
                "Poll, send exact input to, or interrupt an owned provider-backed "
                "PTY shell session; returns bounded output and process status."
            ),
            output_indicators=[
                "stdout",
                "stderr",
                "process_status",
                "session_id",
            ],
        )
    ],
    required_services=[],
    target_protocols=["local"],
    execution_priority=4,
    parallel_compatible=True,
    stealth_level=3,
    estimated_runtime_minutes=1,
    supported_transports=["pty"],
)
_shell_write_stdin_metadata.__dict__["pty_support"] = True
_shell_write_stdin_metadata.__dict__["pty_session_only"] = True
_shell_write_stdin_metadata.__dict__["pty_benefits"] = [
    "polling",
    "exact_stdin",
    "interrupts",
]

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
register_enhanced_tool_metadata(_shell_write_stdin_metadata)
register_enhanced_tool_metadata(_shell_script_metadata)
