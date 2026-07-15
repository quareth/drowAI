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

class ObjdumpMode(str, Enum):
    """Objdump operation modes"""
    DISASSEMBLE = "disassemble"
    HEADERS = "headers"
    SECTIONS = "sections"
    SYMBOLS = "symbols"
    RELOCATIONS = "relocations"
    DYNAMIC = "dynamic"
    FILE_HEADERS = "file-headers"
    ARCH_SPECIFIC = "arch-specific"
    DEBUGGING = "debugging"
    HELP = "help"
    VERSION = "version"

class ObjdumpFormat(str, Enum):
    """Output formats for objdump"""
    ATT = "att"
    INTEL = "intel"

class ObjdumpArgs(BaseToolArgs):
    """Arguments for Objdump tool"""
    mode: ObjdumpMode = Field(default=ObjdumpMode.DISASSEMBLE, description="Operation mode for objdump")
    target: str = Field(
        ...,
        description="Path to the object file or executable to analyze",
    )
    output_format: Optional[ObjdumpFormat] = Field(default=None, description="Output format for disassembly")
    start_address: Optional[str] = Field(default=None, description="Starting address for disassembly (hex format)")
    end_address: Optional[str] = Field(default=None, description="Ending address for disassembly (hex format)")
    section_name: Optional[str] = Field(default=None, description="Specific section to analyze")
    symbol_name: Optional[str] = Field(default=None, description="Specific symbol to analyze")
    architecture: Optional[str] = Field(default=None, description="Target architecture")
    demangle: bool = Field(default=False, description="Demangle symbol names")
    show_raw_insn: bool = Field(default=False, description="Show raw instruction bytes")
    show_source: bool = Field(default=False, description="Show source code with disassembly")
    line_numbers: bool = Field(default=False, description="Show line numbers")
    full_contents: bool = Field(default=False, description="Show full contents of sections")
    wide_output: bool = Field(default=False, description="Use wide output format")
    output_file: Optional[str] = Field(default=None, description="Output file for results")
    verbose: bool = Field(default=False, description="Enable verbose output")
    timeout: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        gt=0,
        description="Timeout in seconds for the operation",
    )

def parse_objdump_output(output_text: str) -> Dict[str, Any]:
    """Parse objdump command output and extract structured information."""
    result = {
        "sections": [],
        "symbols": [],
        "disassembly": [],
        "headers": {},
        "relocations": [],
        "dynamic_entries": [],
        "metadata": {
            "total_sections": 0,
            "total_symbols": 0,
            "total_instructions": 0,
            "architecture": None,
            "file_type": None
        }
    }
    
    try:
        lines = output_text.split('\n')
        current_section = None
        current_symbol = None
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
                
            # Parse file headers
            if line.startswith("file format"):
                result["metadata"]["file_type"] = line.split("file format")[1].strip()
            elif line.startswith("architecture:"):
                result["metadata"]["architecture"] = line.split("architecture:")[1].strip()
            
            # Parse sections
            elif line.startswith("Sections:"):
                current_section = "sections"
            elif current_section == "sections" and line and not line.startswith("Idx"):
                # Parse section line: Idx Name          Size      VMA               LMA               File off  Algn
                parts = line.split()
                if len(parts) >= 6:
                    section = {
                        "index": parts[0],
                        "name": parts[1],
                        "size": parts[2],
                        "vma": parts[3],
                        "lma": parts[4],
                        "file_offset": parts[5],
                        "alignment": parts[6] if len(parts) > 6 else ""
                    }
                    result["sections"].append(section)
                    result["metadata"]["total_sections"] += 1
            
            # Parse symbols
            elif line.startswith("SYMBOL TABLE:"):
                current_section = "symbols"
            elif current_section == "symbols" and line and not line.startswith("Num:"):
                # Parse symbol line: 0000000000000000 l    d  .text	0000000000000000 .text
                parts = line.split()
                if len(parts) >= 6:
                    symbol = {
                        "value": parts[0],
                        "size": parts[1],
                        "type": parts[2],
                        "bind": parts[3],
                        "visibility": parts[4],
                        "section": parts[5],
                        "name": " ".join(parts[6:]) if len(parts) > 6 else ""
                    }
                    result["symbols"].append(symbol)
                    result["metadata"]["total_symbols"] += 1
            
            # Parse disassembly
            elif line.startswith("Disassembly of section"):
                current_section = "disassembly"
                section_name = line.split("section")[1].strip().rstrip(":")
                current_symbol = {"section": section_name, "instructions": []}
            elif current_section == "disassembly" and line:
                # Parse disassembly line: 0000000000000000 <main>: or 0000000000000000:	48 89 e5	mov    %rsp,%rbp
                if "<" in line and ">:" in line:
                    # Function start
                    func_name = line.split("<")[1].split(">")[0]
                    current_symbol["function"] = func_name
                elif ":" in line and "\t" in line:
                    # Instruction line
                    parts = line.split("\t")
                    if len(parts) >= 2:
                        address = parts[0].rstrip(":")
                        instruction = parts[1] if len(parts) == 2 else parts[1] + " " + parts[2]
                        current_symbol["instructions"].append({
                            "address": address,
                            "instruction": instruction
                        })
                        result["metadata"]["total_instructions"] += 1
                elif current_symbol and current_symbol["instructions"]:
                    # End of function, save it
                    result["disassembly"].append(current_symbol)
                    current_symbol = {"instructions": []}
            
            # Parse relocations
            elif line.startswith("RELOCATION RECORDS"):
                current_section = "relocations"
            elif current_section == "relocations" and line and not line.startswith("OFFSET"):
                parts = line.split()
                if len(parts) >= 3:
                    relocation = {
                        "offset": parts[0],
                        "type": parts[1],
                        "value": parts[2]
                    }
                    result["relocations"].append(relocation)
            
            # Parse dynamic entries
            elif line.startswith("DYNAMIC SYMBOL TABLE:"):
                current_section = "dynamic"
            elif current_section == "dynamic" and line and not line.startswith("Num:"):
                parts = line.split()
                if len(parts) >= 6:
                    dynamic = {
                        "value": parts[0],
                        "size": parts[1],
                        "type": parts[2],
                        "bind": parts[3],
                        "visibility": parts[4],
                        "section": parts[5],
                        "name": " ".join(parts[6:]) if len(parts) > 6 else ""
                    }
                    result["dynamic_entries"].append(dynamic)
    
    except Exception as e:
        result["error"] = f"Error parsing objdump output: {str(e)}"
    
    return result

class ObjdumpTool(BaseTool):
    """Objdump Tool for analyzing object files and executables."""
    args_model = ObjdumpArgs

    def build_command(self, args: ObjdumpArgs) -> List[str]:
        cmd = ["objdump"]

        if args.mode == ObjdumpMode.DISASSEMBLE:
            cmd.append("-d")
            if args.output_format:
                cmd.extend(["-M", args.output_format.value])
            if args.symbol_name:
                cmd.extend(["--disassemble-symbols", args.symbol_name])
        elif args.mode == ObjdumpMode.HEADERS:
            cmd.append("-h")
        elif args.mode == ObjdumpMode.SECTIONS:
            cmd.append("-s")
        elif args.mode == ObjdumpMode.SYMBOLS:
            cmd.append("-t")
        elif args.mode == ObjdumpMode.RELOCATIONS:
            cmd.append("-r")
        elif args.mode == ObjdumpMode.DYNAMIC:
            cmd.append("-T")
        elif args.mode == ObjdumpMode.FILE_HEADERS:
            cmd.append("-f")
        elif args.mode == ObjdumpMode.ARCH_SPECIFIC:
            cmd.append("-a")
        elif args.mode == ObjdumpMode.DEBUGGING:
            cmd.append("-g")
        elif args.mode == ObjdumpMode.HELP:
            cmd.append("--help")
        elif args.mode == ObjdumpMode.VERSION:
            cmd.append("--version")

        if args.demangle:
            cmd.append("-C")
        if args.show_raw_insn:
            cmd.append("--show-raw-insn")
        if args.show_source:
            cmd.append("-S")
        if args.line_numbers:
            cmd.append("-l")
        if args.full_contents:
            cmd.append("--full-contents")
        if args.wide_output:
            cmd.append("--wide")
        if args.verbose:
            cmd.append("-v")

        if args.architecture:
            cmd.extend(["-m", args.architecture])
        if args.start_address:
            cmd.extend(["--start-address", args.start_address])
        if args.end_address:
            cmd.extend(["--stop-address", args.end_address])
        if args.section_name:
            cmd.extend(["-j", args.section_name])

        cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: ObjdumpArgs,
    ) -> Dict[str, Any]:
        _ = args
        metadata = parse_objdump_output(stdout)
        if exit_code != 0 and stderr:
            metadata["error"] = stderr.strip()
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: ObjdumpArgs,
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
            artifact_path = os.path.join(ARTIFACTS_DIR, f"objdump_{ts}.txt")
            try:
                os.makedirs(ARTIFACTS_DIR, exist_ok=True)
                with open(artifact_path, "w", encoding="utf-8") as handle:
                    handle.write(stdout)
                artifacts.append(artifact_path)
            except OSError:
                pass

        return artifacts

    def run(self, args: ObjdumpArgs) -> ToolResult:
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

OBJDUMP_EXECUTION_PRIORITY = 7
OBJDUMP_STEALTH_LEVEL = 5
OBJDUMP_ESTIMATED_RUNTIME_MINUTES = 3

register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="reverse_engineering.disassemblers.objdump",
        display_name="Objdump",
        category=ToolCategory.REVERSE_ENGINEERING,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="disassembly",
                description="Disassemble object files or executables (ELF, PE) with configurable formats (Intel or AT&T); returns section metadata, relocations, and instructions; lightweight static analysis.",
                output_indicators=["disassembly"],
            ),
            ToolCapability(
                name="symbol_listing",
                description="List symbols and section metadata",
                output_indicators=["symbols", "sections"],
            ),
        ],
        required_services=[],
        target_protocols=[],
        execution_priority=OBJDUMP_EXECUTION_PRIORITY,
        parallel_compatible=True,
        stealth_level=OBJDUMP_STEALTH_LEVEL,
        estimated_runtime_minutes=OBJDUMP_ESTIMATED_RUNTIME_MINUTES,
    )
)
