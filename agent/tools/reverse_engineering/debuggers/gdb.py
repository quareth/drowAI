"""
GDB (GNU Debugger) Tool Implementation

This module provides a Python interface for GDB, the GNU Debugger. GDB is a
portable debugger that runs on many Unix-like systems and works for many
programming languages including C, C++, and Fortran.

Author: AI Assistant
Date: 2024
"""

import os
import re
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from agent.tools.base_tool import BaseTool
from agent.tools.schemas import BaseToolArgs, ToolResult


DEFAULT_TIMEOUT_SECONDS = 300
ARTIFACTS_DIR = "artifacts"
MIN_ARTIFACT_BYTES = 200


class GDBMode(str, Enum):
    """GDB operation modes"""
    DISASSEMBLE = "disassemble"
    BACKTRACE = "backtrace"
    INFO = "info"
    SYMBOLS = "symbols"
    SCRIPT = "script"


class GDBInfoType(str, Enum):
    """Types of GDB info commands"""
    BREAKPOINTS = "breakpoints"
    REGISTERS = "registers"
    THREADS = "threads"
    FRAME = "frame"
    SYMBOLS = "symbols"
    FUNCTIONS = "functions"
    VARIABLES = "variables"
    SOURCES = "sources"


class GDBArgs(BaseToolArgs):
    """Arguments for GDB tool"""

    target: str = Field(
        ...,
        description="Path to the executable file to analyze",
    )
    mode: GDBMode = Field(
        default=GDBMode.DISASSEMBLE,
        description="Operation mode for GDB"
    )

    info_type: Optional[GDBInfoType] = Field(
        default=None,
        description="Type of information to display"
    )

    function_name: Optional[str] = Field(
        default=None,
        description="Function name to disassemble (defaults to current context)",
    )

    script_file: Optional[str] = Field(
        default=None,
        description="GDB script file to execute"
    )

    gdb_script: Optional[str] = Field(
        default=None,
        description="Inline GDB commands (newline separated)",
    )
    
    output_file: Optional[str] = Field(
        default=None,
        description="Output file for results"
    )
    
    verbose: bool = Field(
        default=False,
        description="Enable verbose output"
    )
    
    quiet: bool = Field(
        default=False,
        description="Suppress GDB startup messages"
    )
    
    timeout: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        gt=0,
        description="Timeout in seconds for the operation"
    )


def parse_gdb_output(output_text: str) -> Dict[str, Any]:
    """
    Parse GDB command output and extract structured information.
    
    Args:
        output_text: Raw output from GDB
        
    Returns:
        Dictionary containing parsed information
    """
    result = {
        "status": "unknown",
        "breakpoints": [],
        "registers": {},
        "variables": {},
        "call_stack": [],
        "disassembly": [],
        "errors": [],
        "warnings": []
    }
    
    try:
        # Parse status
        if "program exited" in output_text.lower():
            result["status"] = "exited"
        elif "breakpoint" in output_text.lower() and "hit" in output_text.lower():
            result["status"] = "breakpoint_hit"
        elif "running" in output_text.lower():
            result["status"] = "running"
        elif "stopped" in output_text.lower():
            result["status"] = "stopped"
        elif "error" in output_text.lower():
            result["status"] = "error"
        
        # Extract breakpoints
        bp_matches = re.findall(r'Breakpoint (\d+) at ([^\n]+)', output_text)
        for bp_num, bp_location in bp_matches:
            result["breakpoints"].append({
                "number": bp_num,
                "location": bp_location.strip()
            })
        
        # Extract registers
        register_matches = re.findall(r'(\w+)\s+([0-9a-fA-Fx]+)', output_text)
        for reg_name, reg_value in register_matches:
            if len(reg_name) <= 4:  # Likely a register name
                result["registers"][reg_name] = reg_value
        
        # Extract variables
        var_matches = re.findall(r'(\w+)\s*=\s*([^\n]+)', output_text)
        for var_name, var_value in var_matches:
            result["variables"][var_name] = var_value.strip()
        
        # Extract call stack
        stack_matches = re.findall(r'#(\d+)\s+([^\n]+)', output_text)
        for frame_num, frame_info in stack_matches:
            result["call_stack"].append({
                "frame": frame_num,
                "info": frame_info.strip()
            })
        
        # Extract disassembly
        asm_matches = re.findall(r'([0-9a-fA-F]+):\s+([^\n]+)', output_text)
        for addr, instruction in asm_matches:
            result["disassembly"].append({
                "address": addr,
                "instruction": instruction.strip()
            })
        
        # Extract errors
        error_matches = re.findall(r'Error: ([^\n]+)', output_text)
        result["errors"] = error_matches
        
        # Extract warnings
        warning_matches = re.findall(r'Warning: ([^\n]+)', output_text)
        result["warnings"] = warning_matches
        
    except Exception as e:
        result["errors"].append(f"Parsing error: {str(e)}")
    
    return result


class GDBTool(BaseTool):
    """
    GDB (GNU Debugger) Tool for debugging and reverse engineering executables.
    
    This tool provides an interface to GDB for debugging programs, setting breakpoints,
    examining memory and variables, and performing various debugging tasks.
    """
    
    args_model = GDBArgs

    def build_command(self, args: GDBArgs) -> List[str]:
        cmd = ["gdb", "--batch"]

        if args.quiet:
            cmd.append("--quiet")

        gdb_commands: List[str] = []

        if args.mode == GDBMode.DISASSEMBLE:
            if args.function_name:
                gdb_commands.append(f"disassemble {args.function_name}")
            else:
                gdb_commands.append("disassemble")
        elif args.mode == GDBMode.BACKTRACE:
            gdb_commands.append("backtrace")
        elif args.mode == GDBMode.INFO:
            info_value = args.info_type.value if args.info_type else GDBInfoType.FUNCTIONS.value
            gdb_commands.append(f"info {info_value}")
        elif args.mode == GDBMode.SYMBOLS:
            gdb_commands.append("info symbols")
        elif args.mode == GDBMode.SCRIPT:
            if args.gdb_script:
                gdb_commands.extend(
                    [line.strip() for line in args.gdb_script.splitlines() if line.strip()]
                )

        for command in gdb_commands:
            cmd.extend(["-ex", command])

        if args.script_file:
            cmd.extend(["-x", args.script_file])

        cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: GDBArgs,
    ) -> Dict[str, Any]:
        _ = args
        metadata = parse_gdb_output(stdout)
        if exit_code != 0 and stderr:
            metadata["errors"].append(stderr.strip())
            metadata["status"] = "error"
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: GDBArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        artifacts: List[str] = []
        if args.output_file:
            artifacts.append(args.output_file)
            try:
                with open(args.output_file, "w", encoding="utf-8") as handle:
                    handle.write(stdout)
            except OSError:
                artifacts.pop()

        if stdout and len(stdout) >= MIN_ARTIFACT_BYTES:
            ts = timestamp if timestamp is not None else int(time.time())
            artifact_path = os.path.join(ARTIFACTS_DIR, f"gdb_{ts}.txt")
            try:
                os.makedirs(ARTIFACTS_DIR, exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as handle:
                    handle.write(stdout)
                artifacts.append(artifact_path)
            except OSError:
                pass

        return artifacts

    def run(self, args: GDBArgs) -> ToolResult:
        cmd = self.build_command(args)
        start = time.time()

        try:
            process = subprocess.run(
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
                metadata={"status": "timeout"},
                execution_time=time.time() - start,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                artifacts=[],
                metadata={"status": "error"},
                execution_time=time.time() - start,
            )

        metadata = self.parse_output(
            stdout=process.stdout,
            stderr=process.stderr,
            exit_code=process.returncode,
            args=args,
        )
        artifacts = self.create_artifacts(process.stdout, args, timestamp=int(start))

        return ToolResult(
            success=self.is_success_exit_code(process.returncode, args),
            exit_code=process.returncode,
            stdout=process.stdout,
            stderr=process.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start,
        )


from agent.tools.enhanced_metadata_registry import (  # noqa: E402
    EnhancedToolMetadata,
    PentestPhase,
    ToolCapability,
    ToolCategory,
    register_enhanced_tool_metadata,
)

GDB_EXECUTION_PRIORITY = 6
GDB_STEALTH_LEVEL = 4
GDB_ESTIMATED_RUNTIME_MINUTES = 5

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="reverse_engineering.debuggers.gdb",
        display_name="GDB",
        category=ToolCategory.REVERSE_ENGINEERING,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="disassembly",
                description="Disassemble and inspect ELF or PE executables in batch mode with scripted GDB commands; returns disassembly, symbol tables, and register or variable state; not for interactive debugging.",
                output_indicators=["disassembly"],
            ),
            ToolCapability(
                name="symbol_inspection",
                description="Inspect symbols and functions using info commands",
                output_indicators=["symbols", "variables"],
            ),
        ],
        required_services=[],
        target_protocols=[],
        execution_priority=GDB_EXECUTION_PRIORITY,
        parallel_compatible=True,
        stealth_level=GDB_STEALTH_LEVEL,
        estimated_runtime_minutes=GDB_ESTIMATED_RUNTIME_MINUTES,
    )
)
