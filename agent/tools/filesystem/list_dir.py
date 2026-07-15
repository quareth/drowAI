from __future__ import annotations

import fnmatch
import os
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .contracts import FilesystemEntry, FsListArgs, FsListResult
from ._error_helpers import build_llm_error
from ._helpers import (
    build_find_command,
    build_ls_command,
    build_tool_result,
    create_output_artifact,
    resolve_workspace_path_safe,
    should_create_artifact,
    to_workspace_relative,
    workspace_root,
)


def _match_globs(name: str, patterns: Iterable[str] | None) -> bool:
    if not patterns:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


class FsListDirTool(BaseTool):
    """
    List directory contents in workspace.
    
    The transport parameter controls execution routing (handled by executor):
    - "file-comm": Execute via Kali container queue
    - "pty": Execute `ls -la {path}` or `find {path}` in PTY session
    - None: Executor auto-selects
    
    PTY Use Cases:
        - Debugging: "What's in this directory?"
        - Verification: See permissions, timestamps, sizes
        - Troubleshooting: Diagnose directory access issues
    
    PTY Command Equivalent:
        ls -la /workspace/task_{task_id}/{path}
    """

    args_model = FsListArgs

    def build_command(self, args: FsListArgs) -> List[str]:
        """Build a PTY-safe command for listing directory contents."""

        workspace = workspace_root()
        base_path = resolve_workspace_path_safe(args.path, workspace=workspace)
        if args.recursive:
            return build_find_command(str(base_path), name_pattern=None, max_depth=None)
        return build_ls_command(str(base_path), long_format=True)

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FsListArgs,
    ) -> Dict[str, object]:
        """Parse list output into structured metadata."""

        line_count = len(stdout.splitlines()) if stdout else 0
        result = FsListResult(entries=[], truncated=False)
        metadata = result.model_dump()
        metadata["entry_count"] = line_count
        metadata["exit_code"] = exit_code
        if exit_code != 0:
            metadata["error"] = stderr or "Directory listing failed"
        return {"fs_list": metadata}

    def create_artifacts(
        self,
        stdout: str,
        args: FsListArgs,
        timestamp: Optional[int] = None,
        stderr: str = "",
    ) -> List[str]:
        """Persist list output as artifacts when needed."""

        created: List[str] = []
        if stdout and should_create_artifact(stdout):
            created.append(create_output_artifact(stdout, "fs_list", timestamp))
        if stderr:
            created.append(create_output_artifact(stderr, "fs_list_errors", timestamp))
        return created

    def run(self, args: FsListArgs) -> ToolResult:
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
                    message=f"Directory '{args.path}' does not exist.",
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
                    message=f"Path '{args.path}' is not a directory. Use filesystem.read_file to read file contents.",
                ),
                metadata={"error": "not_directory"},
                exit_code=1,
            )

        entries: List[FilesystemEntry] = []
        truncated = False

        def should_include(path: Path) -> bool:
            name = path.name
            if not args.include_hidden and name.startswith("."):
                return False
            if not _match_globs(name, args.include_globs):
                return False
            if args.exclude_globs and any(fnmatch.fnmatch(name, pattern) for pattern in args.exclude_globs):
                return False
            return True

        def add_entry(path: Path) -> bool:
            nonlocal truncated
            if len(entries) >= args.max_results:
                truncated = True
                return False
            entry_type = "file"
            size = None
            if path.is_dir():
                entry_type = "directory"
            elif path.is_symlink():
                entry_type = "symlink"
            else:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = None

            try:
                modified = path.stat().st_mtime
            except OSError:
                modified = None

            entries.append(
                FilesystemEntry(
                    path=to_workspace_relative(path, workspace),
                    type=entry_type,
                    size_bytes=size,
                    modified_ts=modified,
                )
            )
            return True

        if args.recursive:
            for root, dirnames, filenames in os.walk(base_path):
                current_dir = Path(root)
                for name in list(dirnames):
                    child = current_dir / name
                    if not should_include(child):
                        continue
                    if not add_entry(child):
                        break
                if len(entries) >= args.max_results:
                    break
                for name in filenames:
                    child = current_dir / name
                    if not should_include(child):
                        continue
                    if not add_entry(child):
                        break
                if len(entries) >= args.max_results:
                    break
        else:
            for child in base_path.iterdir():
                if not should_include(child):
                    continue
                if not add_entry(child):
                    break

        result = FsListResult(entries=entries, truncated=truncated)
        stdout = f"Listed {len(entries)} entries from {args.path}"
        if truncated:
            stdout += " (truncated)"

        listing_text = "\n".join(entry.path for entry in entries)
        metadata = self.parse_output(listing_text, "", 0, args)
        metadata["fs_list"]["entries"] = result.model_dump()["entries"]
        metadata["fs_list"]["truncated"] = truncated
        artifacts = self.create_artifacts(listing_text, args)
        return build_tool_result(
            success=True,
            start=start,
            stdout=stdout,
            metadata=metadata,
            artifacts=artifacts,
        )


