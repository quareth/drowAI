"""Path discovery for filesystem roots inside the active Kali runtime.

This module powers ``filesystem.find_paths``. It searches for files and
directories under one directory root and returns bounded metadata for matches.
Relative paths resolve from ``/workspace`` for container transports, while
absolute paths can address the active Kali runtime. The direct Python
compatibility path remains workspace-local.
"""

from __future__ import annotations

import fnmatch
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .contracts import FilesystemEntry, FsFindArgs, FsFindResult
from ._error_helpers import build_llm_error
from ._helpers import (
    build_find_command,
    build_tool_result,
    create_output_artifact,
    describe_filesystem_scope,
    resolve_workspace_path_safe,
    should_create_artifact,
    to_workspace_relative,
    workspace_root,
)


class FsFindTool(BaseTool):
    """
    Search for files/directories by pattern in the active Kali runtime.
    
    The transport parameter controls execution routing (handled by executor):
    - "file-comm": Execute via Kali container queue
    - "pty": Execute `find {path} -name {pattern}` in PTY session
    - None: Executor auto-selects
    
    PTY Use Cases:
        - Debugging: See search results in real-time
        - Verification: Visual confirmation of matching paths
        - Troubleshooting: Diagnose search pattern issues
    
    PTY Command Equivalent:
        find {path} -name '{pattern}'
    
    Note: Results may be large; PTY output may be truncated.
    """

    args_model = FsFindArgs

    def render_result_output(
        self,
        *,
        args: FsFindArgs,
        stdout: str,
        stderr: str,
    ) -> tuple[str, str]:
        """Render scoped empty find results for command transports."""
        if stdout.strip() or stderr.strip():
            return stdout, stderr

        scope = describe_filesystem_scope(args.path)
        pattern = args.filename_glob or "*"
        return (
            "\n".join(
                [
                    f"No matching paths found for filename_glob {pattern!r} under {scope}.",
                    "Search completed with match_count=0.",
                ]
            ),
            stderr,
        )

    def build_command(self, args: FsFindArgs) -> List[str]:
        """Build a PTY-safe command for path discovery."""

        workspace = workspace_root()
        base_path = resolve_workspace_path_safe(args.path, workspace=workspace)
        command = build_find_command(
            str(base_path),
            name_pattern=args.filename_glob,
            max_depth=args.max_depth,
        )

        if args.file_types:
            type_map = {"file": "f", "directory": "d", "symlink": "l"}
            type_flags = [type_map[file_type] for file_type in args.file_types if file_type in type_map]
            if type_flags:
                type_filters: List[str] = []
                for flag in type_flags:
                    type_filters.extend(["-type", flag, "-o"])
                type_filters.pop()
                command.extend(["("] + type_filters + [")"])

        if args.include_globs:
            include_filters: List[str] = []
            for pattern in args.include_globs:
                include_filters.extend(["-name", pattern, "-o"])
            if include_filters:
                include_filters.pop()
                command.extend(["("] + include_filters + [")"])

        if args.exclude_globs:
            for pattern in args.exclude_globs:
                command.extend(["!", "-name", pattern])

        return command

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FsFindArgs,
    ) -> Dict[str, object]:
        """Parse find output into structured metadata."""

        workspace = workspace_root()
        matches: List[FilesystemEntry] = []
        if exit_code == 0 and stdout:
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                candidate = Path(line)
                if not (candidate.is_absolute() or line.startswith(("/", "\\"))):
                    candidate = resolve_workspace_path_safe(line, workspace=workspace)
                entry_type = "file"
                size = None
                if candidate.is_dir():
                    entry_type = "directory"
                elif candidate.is_symlink():
                    entry_type = "symlink"
                else:
                    try:
                        size = candidate.stat().st_size
                    except OSError:
                        size = None
                matches.append(
                    FilesystemEntry(
                        path=to_workspace_relative(candidate, workspace),
                        type=entry_type,
                        size_bytes=size,
                        modified_ts=None,
                    )
                )

        result = FsFindResult(matches=matches, truncated=False)
        metadata = result.model_dump()
        metadata["exit_code"] = exit_code
        if exit_code != 0:
            metadata["error"] = stderr or "Find command failed"
        return {"fs_find": metadata}

    def create_artifacts(
        self,
        stdout: str,
        args: FsFindArgs,
        timestamp: Optional[int] = None,
        stderr: str = "",
    ) -> List[str]:
        """Persist find output as artifacts when needed."""

        created: List[str] = []
        if stdout and should_create_artifact(stdout):
            created.append(create_output_artifact(stdout, "fs_find", timestamp))
        if stderr:
            created.append(create_output_artifact(stderr, "fs_find_errors", timestamp))
        return created

    def run(self, args: FsFindArgs) -> ToolResult:
        start = time.time()
        workspace = workspace_root()

        try:
            base_path: Path = resolve_workspace_path_safe(args.path, workspace=workspace)
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

        if not base_path.exists():
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="not_found",
                    path=args.path,
                    workspace=workspace,
                    message=f"Search root '{args.path}' does not exist.",
                ),
                metadata={"error": "not_found"},
                exit_code=1,
            )

        if not base_path.is_dir():
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="not_found",
                    path=args.path,
                    workspace=workspace,
                    message=(
                        f"Path '{args.path}' is not a directory. "
                        "Use filesystem.read_file for file content or "
                        "filesystem.search_text to search inside a file."
                    ),
                ),
                metadata={"error": "not_directory"},
                exit_code=1,
            )

        matches: List[FilesystemEntry] = []
        truncated = False

        allowed_types = set(args.file_types or [])

        def type_allowed(path: Path, entry_type: str) -> bool:
            if not allowed_types:
                return True
            return entry_type in allowed_types

        def glob_allowed(path: Path) -> bool:
            rel_name = path.name
            if args.filename_glob and not fnmatch.fnmatch(rel_name, args.filename_glob):
                return False
            if args.include_globs and not any(
                fnmatch.fnmatch(rel_name, pattern) for pattern in args.include_globs
            ):
                return False
            if args.exclude_globs and any(
                fnmatch.fnmatch(rel_name, pattern) for pattern in args.exclude_globs
            ):
                return False
            return True

        max_depth = args.max_depth if args.max_depth is not None else float("inf")

        for root, dirnames, filenames in os.walk(base_path, followlinks=args.follow_symlinks):
            current_dir = Path(root)
            depth = len(current_dir.relative_to(base_path).parts)
            if depth > max_depth:
                dirnames[:] = []
                continue

            # Process directories (unless depth limit prohibits)
            for name in list(dirnames):
                candidate = current_dir / name
                entry_depth = len(candidate.relative_to(base_path).parts)
                if entry_depth < args.min_depth:
                    continue
                entry_type = "directory"
                if not type_allowed(candidate, entry_type):
                    continue
                if not glob_allowed(candidate):
                    continue
                if len(matches) >= args.max_results:
                    truncated = True
                    break
                matches.append(
                    FilesystemEntry(
                        path=to_workspace_relative(candidate, workspace),
                        type=entry_type,
                        size_bytes=None,
                        modified_ts=None,
                    )
                )
            if truncated:
                break

            for name in filenames:
                candidate = current_dir / name
                entry_depth = len(candidate.relative_to(base_path).parts)
                if entry_depth < args.min_depth:
                    continue
                entry_type = "file"
                if candidate.is_symlink():
                    entry_type = "symlink"
                if not type_allowed(candidate, entry_type):
                    continue
                if not glob_allowed(candidate):
                    continue
                if len(matches) >= args.max_results:
                    truncated = True
                    break
                size = None
                try:
                    if candidate.is_file():
                        size = candidate.stat().st_size
                except OSError:
                    size = None

                matches.append(
                    FilesystemEntry(
                        path=to_workspace_relative(candidate, workspace),
                        type=entry_type,
                        size_bytes=size,
                        modified_ts=None,
                    )
                )
            if truncated:
                break

        result = FsFindResult(matches=matches, truncated=truncated)
        stdout = f"Found {len(matches)} matching paths"
        if truncated:
            stdout += " (truncated)"
        if not matches:
            scope = describe_filesystem_scope(args.path)
            pattern = args.filename_glob or "*"
            stdout = "\n".join(
                [
                    f"No matching paths found for filename_glob {pattern!r} under {scope}.",
                    "Search completed with match_count=0.",
                ]
            )

        listing_text = "\n".join(match.path for match in matches)
        metadata = self.parse_output(listing_text, "", 0, args)
        metadata["fs_find"]["matches"] = result.model_dump()["matches"]
        metadata["fs_find"]["truncated"] = truncated
        artifacts = self.create_artifacts(listing_text, args)
        return build_tool_result(
            success=True,
            start=start,
            stdout=stdout,
            metadata=metadata,
            artifacts=artifacts,
        )
