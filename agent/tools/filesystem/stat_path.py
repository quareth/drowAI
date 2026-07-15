from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .contracts import FsStatArgs
from ._error_helpers import build_llm_error
from ._helpers import (
    build_stat_command,
    build_tool_result,
    create_output_artifact,
    resolve_workspace_path_safe,
    should_create_artifact,
    workspace_root,
)


class FsStatTool(BaseTool):
    """
    Get file/directory metadata in workspace.
    
    The transport parameter controls execution routing (handled by executor):
    - "file-comm": Execute via Kali container queue
    - "pty": Execute `stat {path}` in PTY session
    - None: Executor auto-selects
    
    PTY Use Cases:
        - Debugging: See detailed file metadata
        - Troubleshooting: Diagnose permission/access issues
    
    PTY Command Equivalent:
        stat /workspace/task_{task_id}/{path}
    
    Note: PTY output format may vary between containers/platforms.
    """

    args_model = FsStatArgs

    def build_command(self, args: FsStatArgs) -> List[str]:
        """Build a PTY-safe command for stat operations."""

        workspace = workspace_root()
        target = resolve_workspace_path_safe(args.path, workspace=workspace)
        return build_stat_command(str(target))

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FsStatArgs,
    ) -> Dict[str, object]:
        """Parse stat output into structured metadata."""

        if exit_code != 0:
            return {
                "fs_stat": {
                    "path": args.path,
                    "error": stderr or "Stat failed",
                    "exit_code": exit_code,
                }
            }

        size_bytes = None
        for line in stdout.splitlines():
            if line.startswith("Size:"):
                parts = line.split("Size:", 1)
                size_token = parts[1].strip().split(" ")[0]
                if size_token.isdigit():
                    size_bytes = int(size_token)
                break

        return {
            "fs_stat": {
                "path": args.path,
                "type": "file",
                "size_bytes": size_bytes,
                "exit_code": exit_code,
            }
        }

    def create_artifacts(
        self,
        stdout: str,
        args: FsStatArgs,
        timestamp: Optional[int] = None,
        stderr: str = "",
    ) -> List[str]:
        """Persist stat diagnostics as artifacts when needed."""

        created: List[str] = []
        if stdout and should_create_artifact(stdout):
            created.append(create_output_artifact(stdout, "fs_stat", timestamp))
        if stderr:
            created.append(create_output_artifact(stderr, "fs_stat_errors", timestamp))
        return created

    def run(self, args: FsStatArgs) -> ToolResult:
        start = time.time()
        workspace = workspace_root()

        try:
            target: Path = resolve_workspace_path_safe(args.path, workspace=workspace)
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

        if not target.exists():
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="not_found",
                    path=args.path,
                    workspace=workspace,
                    message=f"Path '{args.path}' does not exist.",
                ),
                metadata={"error": "not_found"},
                exit_code=1,
            )

        try:
            stat_result = target.stat(follow_symlinks=args.follow_symlinks)
        except OSError as exc:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="io_error",
                    path=args.path,
                    workspace=workspace,
                    message=f"Failed to stat '{args.path}': {exc}",
                ),
                metadata={"error": "io_error"},
                exit_code=1,
            )

        entry_type = "file"
        if target.is_dir():
            entry_type = "directory"
        elif target.is_symlink():
            entry_type = "symlink"

        stdout = f"Path: {args.path} ({entry_type}, {stat_result.st_size} bytes)"
        metadata = self.parse_output(stdout, "", 0, args)
        artifacts = self.create_artifacts(stdout, args)
        return build_tool_result(
            success=True,
            start=start,
            stdout=stdout,
            metadata=metadata,
            artifacts=artifacts,
        )


from .contracts import FsStatArgs  # noqa: E402  # assign args_model after definition

FsStatTool.args_model = FsStatArgs

