from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .contracts import FsDeleteArgs, FsMutationResult
from ._error_helpers import build_llm_error
from ._helpers import (
    build_rm_command,
    build_tool_result,
    create_output_artifact,
    resolve_workspace_path_safe,
    should_create_artifact,
    workspace_root,
)


class FsDeleteTool(BaseTool):
    """
    Delete files or directories in workspace.
    
    The transport parameter controls execution routing (handled by executor):
    - "file-comm": Execute via Kali container queue
    - "pty": Execute `rm -rf {path}` in PTY session (visible to users)
    - None: Executor auto-selects
    
    PTY Use Cases:
        - Transparency: User can see exactly what's being deleted
        - Verification: Confirm correct file is targeted
        - Troubleshooting: Diagnose deletion/permission issues
    
    Security Guardrails:
        - Workspace isolation enforced (only task files can be deleted)
        - Irreversible operation (no undo)
    
    PTY Command Equivalent:
        rm -rf /workspace/task_{task_id}/{path}  # if recursive
        rm /workspace/task_{task_id}/{path}      # if not recursive
    """

    args_model = FsDeleteArgs

    def build_command(self, args: FsDeleteArgs) -> List[str]:
        """Build a PTY-safe command for deleting a path."""

        workspace = workspace_root()
        target = resolve_workspace_path_safe(args.path, workspace=workspace)
        return build_rm_command(str(target), recursive=args.recursive, force=args.force)

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FsDeleteArgs,
    ) -> Dict[str, object]:
        """Parse delete output into structured metadata."""

        message = f"Deleted {args.path}" if exit_code == 0 else f"Failed to delete {args.path}"
        mutation = FsMutationResult(
            path=args.path,
            action="deleted",
            message=message,
            extra={"exit_code": exit_code, "stderr": stderr} if exit_code != 0 else {},
        )
        return {"fs_delete": mutation.model_dump()}

    def create_artifacts(
        self,
        stdout: str,
        args: FsDeleteArgs,
        timestamp: Optional[int] = None,
        stderr: str = "",
    ) -> List[str]:
        """Persist delete diagnostics as artifacts when needed."""

        created: List[str] = []
        if stdout and should_create_artifact(stdout):
            created.append(create_output_artifact(stdout, "fs_delete", timestamp))
        if stderr:
            created.append(create_output_artifact(stderr, "fs_delete_errors", timestamp))
        return created

    def run(self, args: FsDeleteArgs) -> ToolResult:
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
            if args.force:
                mutation = FsMutationResult(
                    path=args.path,
                    action="deleted",
                    message="Path did not exist; no action taken.",
                )
                stdout = mutation.message or ""
                metadata = self.parse_output(stdout, "", 0, args)
                artifacts = self.create_artifacts(stdout, args)
                return build_tool_result(
                    success=True,
                    start=start,
                    stdout=stdout,
                    metadata=metadata,
                    artifacts=artifacts,
                )
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

        if target.is_dir() and not args.recursive:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="not_empty",
                    path=args.path,
                    workspace=workspace,
                    message=f"'{args.path}' is a directory.",
                ),
                metadata={"error": "requires_recursive"},
                exit_code=1,
            )

        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="io_error",
                    path=args.path,
                    workspace=workspace,
                    message=f"Failed to delete '{args.path}': {exc}",
                ),
                metadata={"error": "io_error"},
                exit_code=1,
            )

        mutation = FsMutationResult(
            path=args.path,
            action="deleted",
            message=f"Deleted {args.path}",
        )
        stdout = mutation.message or ""
        metadata = self.parse_output(stdout, "", 0, args)
        artifacts = self.create_artifacts(stdout, args)
        return build_tool_result(
            success=True,
            start=start,
            stdout=stdout,
            metadata=metadata,
            artifacts=artifacts,
        )


