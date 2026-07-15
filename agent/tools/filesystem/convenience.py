"""Convenience wrapper tools for common filesystem read operations.

 -: These tools provide simplified interfaces for common
file reading patterns, making it easier for LLMs to perform specific operations.

Available Tools:
 FsReadHeadTool: Read first N lines of a file
 FsReadTailTool: Read last N lines of a file
 FsGrepTool: Search for pattern in a file"""

from __future__ import annotations


from pydantic import Field

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .contracts import FsReadArgs, WorkspacePathArgs
from .read_file import FsReadTool


class FsReadHeadArgs(WorkspacePathArgs):
    """Arguments for reading the first N lines of a file.
    
    Simple interface for viewing the beginning of a file.
    
    Examples:
        {"path": "config.yaml"}              # Default: first 100 lines
        {"path": "log.txt", "lines": 50}     # First 50 lines
        {"path": "data.csv", "lines": 10, "show_line_numbers": true}
    """
    lines: int = Field(
        100,
        ge=1,
        le=10000,
        description="Number of lines to read from the beginning of the file.",
    )
    show_line_numbers: bool = Field(
        False,
        description="Prefix each line with its line number.",
    )


class FsReadTailArgs(WorkspacePathArgs):
    """Arguments for reading the last N lines of a file.
    
    Simple interface for viewing the end of a file. Particularly useful
    for log files where recent entries are typically at the end.
    
    Examples:
        {"path": "app.log"}                  # Default: last 100 lines
        {"path": "error.log", "lines": 200}  # Last 200 lines
        {"path": "access.log", "lines": 50, "show_line_numbers": true}
    """
    lines: int = Field(
        100,
        ge=1,
        le=10000,
        description="Number of lines to read from the end of the file.",
    )
    show_line_numbers: bool = Field(
        False,
        description="Prefix each line with its line number.",
    )


class FsGrepArgs(WorkspacePathArgs):
    """Arguments for searching for a pattern in a file.
    
    Simple interface for finding lines that match a pattern.
    Supports regex patterns.
    
    Examples:
        {"path": "log.txt", "pattern": "ERROR"}           # Find ERROR lines
        {"path": "config.yaml", "pattern": "database"}    # Find database config
        {"path": "log.txt", "pattern": "error", "ignore_case": true}  # Case-insensitive
        {"path": "code.py", "pattern": "def\\s+\\w+"}     # Regex: function definitions
    """
    pattern: str = Field(
        ...,
        description="Search pattern (supports regex). Lines containing this pattern will be returned.",
    )
    ignore_case: bool = Field(
        False,
        description="When true, match pattern case-insensitively.",
    )
    max_matches: int = Field(
        200,
        ge=1,
        le=10000,
        description="Maximum number of matching lines to return.",
    )
    show_line_numbers: bool = Field(
        True,  # Default to true for grep since line context is usually important
        description="Prefix each line with its line number (default: true for grep).",
    )


class FsReadHeadTool(BaseTool):
    """Read the first N lines of a file.
    
    This is a convenience wrapper around filesystem.read_file with read_mode='head'.
    Use this when you want to quickly view the beginning of a file.
    
    Common Use Cases:
        - View file structure/headers
        - Check configuration file format
        - Inspect CSV/JSON file structure
        - Quick preview of file contents
    
    Examples:
        {"path": "data.csv", "lines": 10}  # See CSV headers + first rows
        {"path": "config.yaml", "lines": 50}  # View configuration structure
    """
    
    tool_id = "filesystem.read_head"
    args_model = FsReadHeadArgs
    
    def run(self, args: FsReadHeadArgs) -> ToolResult:
        """Read the first N lines using FsReadTool."""
        read_args = FsReadArgs(
            path=args.path,
            read_mode="head",
            num_lines=args.lines,
            include_line_numbers=args.show_line_numbers,
        )
        
        tool = FsReadTool()
        return tool.run(read_args)


class FsReadTailTool(BaseTool):
    """Read the last N lines of a file.
    
    This is a convenience wrapper around filesystem.read_file with read_mode='tail'.
    Use this when you want to view the end of a file, especially useful for logs.
    
    Common Use Cases:
        - View recent log entries
        - Check latest error messages
        - Monitor file activity
        - See recent additions to a file
    
    Examples:
        {"path": "app.log", "lines": 100}  # Last 100 lines of log
        {"path": "error.log", "lines": 50}  # Recent errors
    """
    
    tool_id = "filesystem.read_tail"
    args_model = FsReadTailArgs
    
    def run(self, args: FsReadTailArgs) -> ToolResult:
        """Read the last N lines using FsReadTool."""
        read_args = FsReadArgs(
            path=args.path,
            read_mode="tail",
            num_lines=args.lines,
            include_line_numbers=args.show_line_numbers,
        )
        
        tool = FsReadTool()
        return tool.run(read_args)


class FsGrepTool(BaseTool):
    """Search for a pattern in a file.
    
    This is a convenience wrapper around filesystem.read_file with search capability.
    Use this when you want to find specific content within a file.
    
    Common Use Cases:
        - Find error messages in logs
        - Search for configuration values
        - Locate specific code patterns
        - Filter file content by keyword
    
    Pattern Tips:
        - Simple text: "ERROR" finds lines containing ERROR
        - Regex: "ERROR|WARN" finds ERROR or WARN
        - Anchored: "^import" finds lines starting with import
        - Word boundary: "\\buser\\b" finds whole word "user"
    
    Examples:
        {"path": "app.log", "pattern": "ERROR"}  # Find all errors
        {"path": "config.yaml", "pattern": "port.*\\d+"}  # Find port settings
        {"path": "code.py", "pattern": "def ", "ignore_case": false}  # Find functions
    """
    
    tool_id = "filesystem.grep"
    args_model = FsGrepArgs
    
    def run(self, args: FsGrepArgs) -> ToolResult:
        """Search for pattern using FsReadTool."""
        read_args = FsReadArgs(
            path=args.path,
            search=args.pattern,  # Use simplified 'search' parameter
            case_sensitive=not args.ignore_case,
            num_lines=args.max_matches,
            include_line_numbers=args.show_line_numbers,
        )
        
        tool = FsReadTool()
        return tool.run(read_args)
