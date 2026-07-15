"""Radare2 binary analysis tool and output parser.

Provides a tool wrapper for common radare2 modes and helper parsing for
structured binary-analysis metadata.
"""

import json
import os
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

class Radare2Mode(str, Enum):
    """Radare2 operation modes"""
    ANALYZE = "analyze"
    DISASSEMBLE = "disassemble"
    SEARCH = "search"
    SYMBOLS = "symbols"
    SECTIONS = "sections"
    IMPORTS = "imports"
    EXPORTS = "exports"
    STRINGS = "strings"
    FUNCTIONS = "functions"
    CALLS = "calls"
    REFERENCES = "references"
    PATCH = "patch"
    SCRIPT = "script"
    DEBUG = "debug"

class Radare2Args(BaseToolArgs):
    """Arguments for Radare2 tool"""
    mode: Radare2Mode = Field(default=Radare2Mode.ANALYZE, description="Operation mode for radare2")
    target: str = Field(
        ...,
        description="Path to the binary file to analyze",
    )
    start_address: Optional[str] = Field(default=None, description="Starting address for analysis (hex format)")
    end_address: Optional[str] = Field(default=None, description="Ending address for analysis (hex format)")
    search_pattern: Optional[str] = Field(default=None, description="Pattern to search for in the binary")
    function_name: Optional[str] = Field(default=None, description="Specific function to analyze")
    section_name: Optional[str] = Field(default=None, description="Specific section to analyze")
    script_file: Optional[str] = Field(default=None, description="Radare2 script file to execute")
    commands: Optional[str] = Field(default=None, description="Radare2 commands to execute")
    architecture: Optional[str] = Field(default=None, description="Target architecture")
    bits: Optional[int] = Field(default=None, gt=0, description="Target architecture bits (32 or 64)")
    endian: Optional[str] = Field(default=None, description="Endianness (little or big)")
    output_file: Optional[str] = Field(default=None, description="Output file for results")
    verbose: bool = Field(default=False, description="Enable verbose output")
    quiet: bool = Field(default=False, description="Suppress output")
    json_output: bool = Field(default=False, description="Enable JSON output when supported")
    timeout: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        gt=0,
        description="Timeout in seconds for the operation",
    )

def parse_radare2_output(output_text: str) -> Dict[str, Any]:
    """Parse radare2 command output and extract structured information."""
    result = {
        "analysis": {},
        "functions": [],
        "symbols": [],
        "sections": [],
        "imports": [],
        "exports": [],
        "strings": [],
        "disassembly": [],
        "search_results": [],
        "metadata": {
            "total_functions": 0,
            "total_symbols": 0,
            "total_sections": 0,
            "total_strings": 0,
            "architecture": None,
            "file_type": None,
            "entry_point": None
        }
    }
    
    try:
        lines = output_text.split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Parse file information
            if line.startswith("file:"):
                result["metadata"]["file_type"] = line.split("file:")[1].strip()
            elif line.startswith("arch:"):
                result["metadata"]["architecture"] = line.split("arch:")[1].strip()
            elif line.startswith("entry:"):
                result["metadata"]["entry_point"] = line.split("entry:")[1].strip()
            
            # Parse functions
            elif line.startswith("f ") and " " in line:
                # Function line: f sym.main 0x4004d0 5
                parts = line.split()
                if len(parts) >= 3:
                    function = {
                        "name": parts[1],
                        "address": parts[2],
                        "size": parts[3] if len(parts) > 3 else "0"
                    }
                    result["functions"].append(function)
                    result["metadata"]["total_functions"] += 1
            
            # Parse symbols
            elif line.startswith("f ") and "sym." in line:
                # Symbol line: f sym.printf 0x4003c0 6
                parts = line.split()
                if len(parts) >= 3:
                    symbol = {
                        "name": parts[1],
                        "address": parts[2],
                        "size": parts[3] if len(parts) > 3 else "0"
                    }
                    result["symbols"].append(symbol)
                    result["metadata"]["total_symbols"] += 1
            
            # Parse sections
            elif line.startswith("S ") and " " in line:
                # Section line: S 0x400000 0x400000 r-x 0x1000 0x1000 .text
                parts = line.split()
                if len(parts) >= 6:
                    section = {
                        "address": parts[1],
                        "size": parts[2],
                        "permissions": parts[3],
                        "offset": parts[4],
                        "size_hex": parts[5],
                        "name": parts[6] if len(parts) > 6 else ""
                    }
                    result["sections"].append(section)
                    result["metadata"]["total_sections"] += 1
            
            # Parse imports
            elif line.startswith("i ") and "imp." in line:
                # Import line: i 0x4003c0 0x4003c0 6 6 sym.imp.printf
                parts = line.split()
                if len(parts) >= 4:
                    import_entry = {
                        "address": parts[1],
                        "size": parts[2],
                        "name": parts[4] if len(parts) > 4 else ""
                    }
                    result["imports"].append(import_entry)
            
            # Parse exports
            elif line.startswith("f ") and "exp." in line:
                # Export line: f exp.main 0x4004d0 5
                parts = line.split()
                if len(parts) >= 3:
                    export = {
                        "name": parts[1],
                        "address": parts[2],
                        "size": parts[3] if len(parts) > 3 else "0"
                    }
                    result["exports"].append(export)
            
            # Parse strings
            elif line.startswith("s ") and "str." in line:
                # String line: s 0x4006a0 0x4006a0 0x1b 0x1b str.Hello_World
                parts = line.split()
                if len(parts) >= 5:
                    string_entry = {
                        "address": parts[1],
                        "size": parts[2],
                        "length": parts[3],
                        "name": parts[4] if len(parts) > 4 else ""
                    }
                    result["strings"].append(string_entry)
                    result["metadata"]["total_strings"] += 1
            
            # Parse disassembly
            elif line.startswith("|") and ":" in line:
                # Disassembly line: | 0x4004d0:  55                    push   rbp
                parts = line.split(":", 1)
                if len(parts) == 2:
                    address = parts[0].strip()
                    instruction = parts[1].strip()
                    result["disassembly"].append({
                        "address": address,
                        "instruction": instruction
                    })
            
            # Parse search results
            elif line.startswith("0x") and " " in line:
                # Search result line: 0x4006a0 48 65 6c 6c 6f 20 57 6f 72 6c 64 00  Hello World.
                parts = line.split()
                if len(parts) >= 2:
                    search_result = {
                        "address": parts[0],
                        "bytes": parts[1:-1] if len(parts) > 2 else [],
                        "ascii": parts[-1] if len(parts) > 1 else ""
                    }
                    result["search_results"].append(search_result)
    
    except Exception as e:
        result["error"] = f"Error parsing radare2 output: {str(e)}"
    
    return result

class Radare2Tool(BaseTool):
    """Radare2 Tool for reverse engineering and binary analysis."""
    args_model = Radare2Args

    def _command_suffix(self, base_command: str, args: Radare2Args) -> str:
        if not args.json_output:
            return base_command
        if base_command.endswith("j"):
            return base_command
        if " " in base_command:
            command, rest = base_command.split(" ", 1)
            return f"{command}j {rest}"
        return f"{base_command}j"

    def build_command(self, args: Radare2Args) -> List[str]:
        cmd = ["r2", "-q"]

        if args.verbose:
            cmd.append("-v")
        if args.architecture:
            cmd.extend(["-a", args.architecture])
        if args.bits:
            cmd.extend(["-b", str(args.bits)])
        if args.endian:
            cmd.extend(["-e", args.endian])
        if args.script_file:
            cmd.extend(["-i", args.script_file])

        r2_commands: List[str] = []

        if args.mode == Radare2Mode.ANALYZE:
            r2_commands.append("aaa")
            r2_commands.append(self._command_suffix("aflj", args))
        elif args.mode == Radare2Mode.DISASSEMBLE:
            if args.start_address:
                r2_commands.append(f"s {args.start_address}")
            r2_commands.append(self._command_suffix("pdf", args))
        elif args.mode == Radare2Mode.SEARCH:
            if args.search_pattern:
                r2_commands.append(f"/x {args.search_pattern}")
            else:
                r2_commands.append("/x")
        elif args.mode == Radare2Mode.SYMBOLS:
            r2_commands.append(self._command_suffix("is", args))
        elif args.mode == Radare2Mode.SECTIONS:
            r2_commands.append(self._command_suffix("iS", args))
        elif args.mode == Radare2Mode.IMPORTS:
            r2_commands.append(self._command_suffix("ii", args))
        elif args.mode == Radare2Mode.EXPORTS:
            r2_commands.append(self._command_suffix("iE", args))
        elif args.mode == Radare2Mode.STRINGS:
            r2_commands.append(self._command_suffix("iz", args))
        elif args.mode == Radare2Mode.FUNCTIONS:
            r2_commands.append(self._command_suffix("afl", args))
        elif args.mode == Radare2Mode.CALLS:
            if args.function_name:
                r2_commands.append(f"s {args.function_name}")
            r2_commands.append(self._command_suffix("ax", args))
        elif args.mode == Radare2Mode.REFERENCES:
            if args.search_pattern:
                r2_commands.append(self._command_suffix(f"axt {args.search_pattern}", args))
            else:
                r2_commands.append(self._command_suffix("axt", args))
        elif args.mode == Radare2Mode.PATCH:
            if args.start_address and args.search_pattern:
                r2_commands.append(f"s {args.start_address}")
                r2_commands.append(f"wx {args.search_pattern}")
        elif args.mode == Radare2Mode.DEBUG:
            r2_commands.append("db")
        elif args.mode == Radare2Mode.SCRIPT:
            if args.commands:
                r2_commands.extend([c.strip() for c in args.commands.split(";") if c.strip()])

        if args.commands and args.mode != Radare2Mode.SCRIPT:
            r2_commands.extend([c.strip() for c in args.commands.split(";") if c.strip()])

        r2_commands.append("q")
        for command in r2_commands:
            cmd.extend(["-c", command])
        cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: Radare2Args,
    ) -> Dict[str, Any]:
        metadata: Dict[str, Any]
        if args.json_output:
            try:
                metadata = json.loads(stdout) if stdout.strip() else {}
            except json.JSONDecodeError:
                metadata = parse_radare2_output(stdout)
        else:
            metadata = parse_radare2_output(stdout)

        if exit_code != 0 and stderr:
            metadata["error"] = stderr.strip()
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: Radare2Args,
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
            extension = "json" if args.json_output else "txt"
            artifact_path = os.path.join(ARTIFACTS_DIR, f"radare2_{ts}.{extension}")
            try:
                os.makedirs(ARTIFACTS_DIR, exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as handle:
                    handle.write(stdout)
                artifacts.append(artifact_path)
            except OSError:
                pass

        return artifacts

    def run(self, args: Radare2Args) -> ToolResult:
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

RADARE2_EXECUTION_PRIORITY = 8
RADARE2_STEALTH_LEVEL = 4
RADARE2_ESTIMATED_RUNTIME_MINUTES = 8

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="reverse_engineering.disassemblers.radare2",
        display_name="Radare2",
        category=ToolCategory.REVERSE_ENGINEERING,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="binary_analysis",
                description="Analyze executables (ELF, PE, Mach-O) with radare2 commands and scripts; returns functions, symbols, sections, imports, and disassembly; not for live debugging.",
                output_indicators=["functions", "symbols", "sections"],
            ),
            ToolCapability(
                name="disassembly",
                description="Disassemble functions and inspect instructions",
                output_indicators=["disassembly"],
            ),
        ],
        required_services=[],
        target_protocols=[],
        execution_priority=RADARE2_EXECUTION_PRIORITY,
        parallel_compatible=True,
        stealth_level=RADARE2_STEALTH_LEVEL,
        estimated_runtime_minutes=RADARE2_ESTIMATED_RUNTIME_MINUTES,
    )
)
