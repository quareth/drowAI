"""Surgical line-level file editing tool.

This tool enables targeted edits to specific lines without rewriting entire files.
Much more efficient than read_file + write_file for:
- Fixing a typo on a specific line
- Updating a configuration value
- Inserting a new function at a specific location
- Removing obsolete code sections

The tool returns a diff-style preview showing what changed.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .contracts import FsEditLinesArgs, FsEditResult
from ._error_helpers import build_llm_error
from ._helpers import (
    build_tool_result,
    resolve_workspace_path_safe,
    workspace_root,
)


class FsEditLinesTool(BaseTool):
    """
    Edit specific lines in a file without rewriting entire content.
    
    This tool enables surgical edits - modifying specific lines without
    reading and rewriting the entire file. Much more efficient for:
    - Fixing a typo on line 42
    - Updating a configuration value
    - Inserting a new function at a specific location
    - Removing obsolete code sections
    
    The tool returns a diff-style preview showing what changed.
    
    Modes:
        - replace: Replace lines start_line to end_line with new_content
        - insert: Insert new_content before start_line (end_line ignored)
        - delete: Delete lines start_line to end_line (new_content ignored)
    
    Security Guardrails:
        - All paths validated with resolve_workspace_path_safe()
        - Workspace isolation enforced (cannot edit outside task workspace)
        - Path traversal blocked (.., absolute paths)
        - Line numbers validated (must be >= 1)
    
    PTY Transport:
        - Delete mode: sed -i '{start},{end}d' {path}
        - Replace/insert modes require Python (heredoc complexity)
    
    Examples:
        {"path": "config.yaml", "start_line": 10, "end_line": 15, 
         "new_content": "key: new_value", "mode": "replace"}
        {"path": "script.sh", "start_line": 5, "new_content": "# New comment",
         "mode": "insert"}
        {"path": "log.txt", "start_line": 20, "end_line": 25, "mode": "delete"}
    """

    args_model = FsEditLinesArgs

    def build_command(self, args: FsEditLinesArgs) -> List[str]:
        """Build a PTY-safe command for line editing.
        
        Only delete mode is supported via PTY (uses sed).
        Replace/insert modes require Python implementation due to heredoc complexity.
        """
        if args.mode != "delete":
            raise ValueError(
                f"PTY transport only supports delete mode. "
                f"Mode '{args.mode}' requires direct execution."
            )

        workspace = workspace_root()
        target = resolve_workspace_path_safe(args.path, workspace=workspace)
        
        end_line = args.end_line or args.start_line
        # sed -i '{start},{end}d' file
        return ["sed", "-i", f"{args.start_line},{end_line}d", str(target)]

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FsEditLinesArgs,
    ) -> Dict[str, object]:
        """Parse edit output into structured metadata."""
        end_line = args.end_line or args.start_line
        lines_affected = end_line - args.start_line + 1 if args.mode != "insert" else 0
        
        result = FsEditResult(
            path=args.path,
            mode=args.mode,
            start_line=args.start_line,
            end_line=end_line,
            lines_affected=lines_affected,
            new_line_count=0,  # Unknown from PTY output
            backup_created=args.backup,
            diff_preview=stdout if stdout else None,
        )
        return {"fs_edit": result.model_dump()}

    def create_artifacts(
        self,
        stdout: str,
        args: FsEditLinesArgs,
        timestamp: Optional[int] = None,
        stderr: str = "",
    ) -> List[str]:
        """Edit operations don't typically create artifacts."""
        return []

    def run(self, args: FsEditLinesArgs) -> ToolResult:
        """Execute the line edit operation."""
        start = time.time()
        workspace = workspace_root()

        # Validate path
        try:
            target = resolve_workspace_path_safe(args.path, workspace=workspace)
        except ValueError as exc:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="path_out_of_workspace",
                    path=args.path,
                    workspace=workspace,
                    message=str(exc),
                ),
                metadata={"error": "path_out_of_workspace"},
            )

        # Check file exists
        if not target.exists():
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="not_found",
                    path=args.path,
                    workspace=workspace,
                    message=f"File '{args.path}' not found.",
                ),
                metadata={"error": "not_found"},
            )

        if target.is_dir():
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="is_directory",
                    path=args.path,
                    workspace=workspace,
                    message=f"'{args.path}' is a directory, not a file.",
                ),
                metadata={"error": "is_directory"},
            )

        # Read current content
        try:
            original_text = target.read_text(encoding="utf-8")
            lines = original_text.splitlines(keepends=True)
        except UnicodeDecodeError as exc:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="read_error",
                    path=args.path,
                    workspace=workspace,
                    message=f"Failed to read file as UTF-8: {exc}. Binary files cannot be edited with this tool.",
                ),
                metadata={"error": "encoding_error"},
            )
        except OSError as exc:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="read_error",
                    path=args.path,
                    workspace=workspace,
                    message=f"Failed to read file: {exc}",
                ),
                metadata={"error": "read_error"},
            )

        total_lines = len(lines)
        end_line = args.end_line or args.start_line

        # Validate line numbers
        if args.start_line > total_lines + 1:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="line_out_of_range",
                    path=args.path,
                    workspace=workspace,
                    message=f"start_line {args.start_line} exceeds file length ({total_lines} lines).",
                    context={"total_lines": total_lines},
                ),
                metadata={"error": "line_out_of_range", "total_lines": total_lines},
            )

        if end_line < args.start_line:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="invalid_range",
                    path=args.path,
                    workspace=workspace,
                    message=f"end_line ({end_line}) must be >= start_line ({args.start_line}).",
                    context={"total_lines": total_lines},
                ),
                metadata={"error": "invalid_range"},
            )

        # Create backup if requested
        backup_path: Optional[Path] = None
        if args.backup:
            backup_path = target.with_suffix(target.suffix + ".bak")
            try:
                shutil.copy2(target, backup_path)
            except OSError as exc:
                return build_tool_result(
                    success=False,
                    start=start,
                    stderr=build_llm_error(
                        error_type="io_error",
                        path=args.path,
                        workspace=workspace,
                        message=f"Failed to create backup: {exc}",
                    ),
                    metadata={"error": "backup_failed"},
                )

        # Prepare new content lines
        new_content_lines: List[str] = []
        if args.new_content:
            new_content_lines = args.new_content.splitlines(keepends=True)
            # Ensure the last line ends with newline
            if new_content_lines and not new_content_lines[-1].endswith("\n"):
                new_content_lines[-1] += "\n"
        elif args.new_content == "" and args.mode != "delete":
            # Empty string for replace means replace with nothing (effectively delete)
            new_content_lines = []

        # Store original for diff
        original_lines = lines.copy()

        # Apply edit based on mode
        if args.mode == "replace":
            # Replace lines start_line to end_line with new content
            start_idx = args.start_line - 1
            end_idx = min(end_line, len(lines))
            lines = lines[:start_idx] + new_content_lines + lines[end_idx:]

        elif args.mode == "insert":
            # Insert before start_line
            start_idx = args.start_line - 1
            # Handle inserting at end of file
            if start_idx >= len(lines):
                lines = lines + new_content_lines
            else:
                lines = lines[:start_idx] + new_content_lines + lines[start_idx:]

        elif args.mode == "delete":
            # Delete lines start_line to end_line
            start_idx = args.start_line - 1
            end_idx = min(end_line, len(lines))
            lines = lines[:start_idx] + lines[end_idx:]

        # Write result
        try:
            target.write_text("".join(lines), encoding="utf-8")
        except OSError as exc:
            # Restore from backup if we made one
            if backup_path and backup_path.exists():
                try:
                    shutil.copy2(backup_path, target)
                except OSError:
                    pass  # Best effort restore
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="write_error",
                    path=args.path,
                    workspace=workspace,
                    message=f"Failed to write file: {exc}",
                ),
                metadata={"error": "write_error"},
            )

        # Build diff preview
        diff_preview = self._build_diff_preview(
            original_lines, lines, args.start_line, end_line, args.mode
        )

        lines_affected = end_line - args.start_line + 1 if args.mode != "insert" else 0

        result = FsEditResult(
            path=args.path,
            mode=args.mode,
            start_line=args.start_line,
            end_line=end_line,
            lines_affected=lines_affected,
            new_line_count=len(lines),
            backup_created=args.backup,
            diff_preview=diff_preview,
        )

        mode_desc = {
            "replace": f"replaced lines {args.start_line}-{end_line}",
            "insert": f"inserted before line {args.start_line}",
            "delete": f"deleted lines {args.start_line}-{end_line}",
        }

        stdout_msg = (
            f"Successfully edited {args.path} ({mode_desc.get(args.mode, args.mode)})\n"
            f"File now has {len(lines)} lines.\n\n"
            f"{diff_preview}"
        )

        return build_tool_result(
            success=True,
            start=start,
            stdout=stdout_msg,
            metadata={"fs_edit": result.model_dump()},
        )

    def _build_diff_preview(
        self,
        original: List[str],
        modified: List[str],
        start_line: int,
        end_line: int,
        mode: str,
    ) -> str:
        """Build a simple diff preview showing changes.
        
        Shows context lines around the change area with +/- markers.
        """
        parts = ["Changes:"]
        context_lines = 2

        if mode == "delete":
            # Show what was deleted
            preview_start = max(0, start_line - 1 - context_lines)
            preview_end = min(len(original), end_line + context_lines)
            
            for i in range(preview_start, preview_end):
                line_num = i + 1
                line_content = original[i].rstrip("\n") if i < len(original) else ""
                if start_line <= line_num <= end_line:
                    parts.append(f"- {line_num:4d}| {line_content}")
                else:
                    parts.append(f"  {line_num:4d}| {line_content}")
        else:
            # Show what was added/replaced
            # Calculate where the new content starts in the modified file
            new_content_start = start_line - 1
            new_content_lines = len(modified) - len(original) + (end_line - start_line + 1) if mode == "replace" else len(modified) - len(original)
            
            preview_start = max(0, new_content_start - context_lines)
            preview_end = min(len(modified), new_content_start + max(new_content_lines, 1) + context_lines)
            
            for i in range(preview_start, preview_end):
                line_num = i + 1
                line_content = modified[i].rstrip("\n") if i < len(modified) else ""
                
                # Determine if this line is part of the new content
                is_new = False
                if mode == "insert":
                    is_new = new_content_start <= i < new_content_start + new_content_lines
                elif mode == "replace":
                    lines_added = len(modified) - len(original) + (end_line - start_line + 1)
                    is_new = new_content_start <= i < new_content_start + lines_added
                
                if is_new:
                    parts.append(f"+ {line_num:4d}| {line_content}")
                else:
                    parts.append(f"  {line_num:4d}| {line_content}")

        return "\n".join(parts)
