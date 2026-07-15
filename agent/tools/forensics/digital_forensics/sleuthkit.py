"""Sleuth Kit - Digital forensics analysis toolkit for file system analysis and data recovery."""

from __future__ import annotations

import os
import re
import subprocess
import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import Field

from ...base_tool import BaseTool
from ...schemas import BaseToolArgs, ToolResult


DEFAULT_TIMEOUT_SECONDS = 300
ARTIFACT_OUTPUT_MIN_CHARS = 200


class SleuthKitCommand(str, Enum):
    """Supported Sleuth Kit CLI commands."""

    MMLS = "mmls"
    FLS = "fls"
    ISTAT = "istat"
    ICAT = "icat"
    BLKSTAT = "blkstat"
    BLKCAT = "blkcat"
    FSSTAT = "fsstat"


class FileSystemType(str, Enum):
    """Supported file system types."""

    FAT12 = "fat12"
    FAT16 = "fat16"
    FAT32 = "fat32"
    NTFS = "ntfs"
    EXT2 = "ext2"
    EXT3 = "ext3"
    EXT4 = "ext4"
    HFS = "hfs"
    HFS_PLUS = "hfs+"
    ISO9660 = "iso9660"
    UFS = "ufs"


class ImageType(str, Enum):
    """Image types supported by Sleuth Kit."""

    RAW = "raw"
    AFF = "aff"
    EWF = "ewf"
    VMDK = "vmdk"
    VHD = "vhd"
    VHDX = "vhdx"


class SleuthKitArgs(BaseToolArgs):
    """Arguments for Sleuth Kit CLI tools."""

    command: SleuthKitCommand = Field(
        SleuthKitCommand.MMLS,
        description="Sleuth Kit command to execute",
    )
    file_system_type: Optional[FileSystemType] = Field(
        None,
        description="File system type to use (-f)",
    )
    image_type: Optional[ImageType] = Field(
        None,
        description="Image type to use (-i)",
    )
    offset: Optional[int] = Field(
        None,
        ge=0,
        description="Offset in sectors to start analysis (-o)",
    )
    inode: Optional[int] = Field(
        None,
        ge=0,
        description="Inode number for istat/icat or starting inode for fls",
    )
    block_number: Optional[int] = Field(
        None,
        ge=0,
        description="Block number for blkstat/blkcat",
    )
    recursive: bool = Field(
        False,
        description="Recursively list directories (fls -r)",
    )
    path_prefix: Optional[str] = Field(
        None,
        description="Prefix path for fls output (-m)",
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT_SECONDS,
        description="Timeout in seconds",
        ge=60,
        le=3600,
    )


def _parse_key_value_lines(output_text: str) -> Dict[str, str]:
    info: Dict[str, str] = {}
    for line in output_text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key:
            info[key] = value
    return info


def _parse_mmls_output(output_text: str) -> Dict[str, Any]:
    partitions = []
    for line in output_text.splitlines():
        if not re.match(r"^\d+:\s+", line):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        partitions.append(
            {
                "partition": parts[0].rstrip(":"),
                "start_sector": parts[1],
                "length": parts[2],
                "description": " ".join(parts[3:]),
            }
        )
    return {
        "partitions_found": len(partitions),
        "partition_table": partitions,
    }


def _parse_fls_output(output_text: str) -> Dict[str, Any]:
    entries = [line.strip() for line in output_text.splitlines() if line.strip()]
    deleted_entries = [line for line in entries if line.startswith("*")]
    return {
        "entries_found": len(entries),
        "deleted_entries": len(deleted_entries),
        "entries": entries,
    }


def parse_sleuthkit_output(output_text: str, command: SleuthKitCommand) -> Dict[str, Any]:
    """Parse Sleuth Kit output into structured metadata."""
    metadata: Dict[str, Any] = {
        "command_executed": command.value,
        "output_lines": len(output_text.splitlines()) if output_text else 0,
        "has_output": bool(output_text.strip()),
    }

    if not output_text.strip():
        return metadata

    if command == SleuthKitCommand.MMLS:
        metadata.update(_parse_mmls_output(output_text))
    elif command == SleuthKitCommand.FLS:
        metadata.update(_parse_fls_output(output_text))
    elif command in (SleuthKitCommand.ISTAT, SleuthKitCommand.BLKSTAT, SleuthKitCommand.FSSTAT):
        metadata["details"] = _parse_key_value_lines(output_text)
    else:
        metadata["output_size"] = len(output_text)

    return metadata


class SleuthKitTool(BaseTool):
    """Sleuth Kit - Digital forensics analysis toolkit for file system analysis and data recovery."""

    args_model = SleuthKitArgs

    def build_command(self, args: SleuthKitArgs) -> List[str]:
        cmd: List[str] = [args.command.value]

        if args.file_system_type:
            cmd.extend(["-f", args.file_system_type.value])

        if args.image_type:
            cmd.extend(["-i", args.image_type.value])

        if args.offset is not None:
            cmd.extend(["-o", str(args.offset)])

        if args.command == SleuthKitCommand.FLS:
            if args.recursive:
                cmd.append("-r")
            if args.path_prefix:
                cmd.extend(["-m", args.path_prefix])
            cmd.append(args.target)
            if args.inode is not None:
                cmd.append(str(args.inode))
            return cmd

        if args.command in (SleuthKitCommand.ISTAT, SleuthKitCommand.ICAT):
            if args.inode is None:
                raise ValueError("inode is required for istat/icat")
            cmd.extend([args.target, str(args.inode)])
            return cmd

        if args.command in (SleuthKitCommand.BLKSTAT, SleuthKitCommand.BLKCAT):
            if args.block_number is None:
                raise ValueError("block_number is required for blkstat/blkcat")
            cmd.extend([args.target, str(args.block_number)])
            return cmd

        cmd.append(args.target)
        return cmd

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: SleuthKitArgs,
    ) -> Dict[str, Any]:
        metadata = parse_sleuthkit_output(stdout or "", args.command)
        if stderr:
            metadata["stderr"] = stderr[:2000]
        metadata["exit_code"] = exit_code
        return metadata

    def create_artifacts(
        self,
        stdout: str,
        args: SleuthKitArgs,
        timestamp: Optional[int] = None,
    ) -> List[str]:
        if not stdout or len(stdout) < ARTIFACT_OUTPUT_MIN_CHARS:
            return []

        ts = int(timestamp or time.time())
        os.makedirs("artifacts", exist_ok=True)
        artifact_path = f"artifacts/sleuthkit_{args.command.value}_{ts}.txt"
        try:
            with open(artifact_path, "w", encoding="utf-8") as handle:
                handle.write(stdout)
        except OSError:
            return []
        return [artifact_path]

    def run(self, args: SleuthKitArgs) -> ToolResult:
        start = time.time()
        try:
            cmd = self.build_command(args)
            proc = subprocess.run(
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
                metadata={},
                execution_time=time.time() - start,
            )

        metadata = self.parse_output(
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.returncode,
            args=args,
        )
        artifacts = self.create_artifacts(proc.stdout, args=args, timestamp=int(start))
        success = self.is_success_exit_code(proc.returncode, args)

        return ToolResult(
            success=success,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            artifacts=artifacts,
            metadata=metadata,
            execution_time=time.time() - start,
        )


# ---------------------------------------------------------------------------
# Tool Metadata Registration
# ---------------------------------------------------------------------------
from ...enhanced_metadata_registry import (  # noqa: E402
    register_enhanced_tool_metadata,
    EnhancedToolMetadata,
    ToolCapability,
    ToolCategory,
    PentestPhase,
)


register_enhanced_tool_metadata(
    EnhancedToolMetadata(
        tool_id="forensics.digital_forensics.sleuthkit",
        display_name="Sleuth Kit",
        category=ToolCategory.FORENSICS,
        applicable_phases=[PentestPhase.POST_EXPLOITATION],
        capabilities=[
            ToolCapability(
                name="filesystem_analysis",
                description="Inspect disk images with Sleuth Kit CLI (mmls, fls, istat, icat); returns partition layout, inode metadata, and filesystem details; read-only block-device inspection.",
                output_indicators=["partition_table", "entries_found", "inode"],
            )
        ],
        required_services=[],
        target_protocols=[],
        execution_priority=7,
        parallel_compatible=True,
        stealth_level=1,
        estimated_runtime_minutes=10,
    )
)
