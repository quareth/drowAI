from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .contracts import FsMakeDirArgs, FsMutationResult
from ._error_helpers import build_llm_error
from ._helpers import (
    build_mkdir_command,
    build_tool_result,
    create_output_artifact,
    resolve_workspace_path_safe,
    should_create_artifact,
    workspace_root,
)


class FsMakeDirTool(BaseTool):
    """
    Create directories in workspace.
    
    The transport parameter controls execution routing (handled by executor):
    - "file-comm": Execute via Kali container queue
    - "pty": Execute `mkdir -p {path}` in PTY session
    - None: Executor auto-selects
    
    PTY Use Cases:
        - Verification: User wants to see directory creation
        - Troubleshooting: Diagnose permission issues
    
    PTY Command Equivalent:
        mkdir -p /workspace/task_{task_id}/{path}
    """

    args_model = FsMakeDirArgs

    def build_command(self, args: FsMakeDirArgs) -> List[str]:
        """Build a PTY-safe command for creating directories."""

        workspace = workspace_root()
        target = resolve_workspace_path_safe(args.path, workspace=workspace)
        return build_mkdir_command(str(target), parents=args.parents)

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FsMakeDirArgs,
    ) -> Dict[str, object]:
        """Parse mkdir output into structured metadata."""

        message = (
            f"Created directory {args.path}" if exit_code == 0 else f"Failed to create {args.path}"
        )
        mutation = FsMutationResult(
            path=args.path,
            action="created",
            message=message,
            extra={"exit_code": exit_code, "stderr": stderr} if exit_code != 0 else {},
        )
        return {"fs_mkdir": mutation.model_dump()}

    def create_artifacts(
        self,
        stdout: str,
        args: FsMakeDirArgs,
        timestamp: Optional[int] = None,
        stderr: str = "",
    ) -> List[str]:
        """Persist mkdir diagnostics as artifacts when needed."""

        created: List[str] = []
        if stdout and should_create_artifact(stdout):
            created.append(create_output_artifact(stdout, "fs_mkdir", timestamp))
        if stderr:
            created.append(create_output_artifact(stderr, "fs_mkdir_errors", timestamp))
        return created

    def run(self, args: FsMakeDirArgs) -> ToolResult:
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

        if target.exists():
            if not args.exist_ok:
                return build_tool_result(
                    success=False,
                    start=start,
                    stderr=build_llm_error(
                        error_type="already_exists",
                        path=args.path,
                        workspace=workspace,
                        message=f"Directory '{args.path}' already exists.",
                    ),
                    metadata={"error": "already_exists"},
                    exit_code=1,
                )
            mutation = FsMutationResult(
                path=args.path,
                action="metadata",
                message=f"Directory '{args.path}' already exists.",
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

        try:
            target.mkdir(parents=args.parents, exist_ok=True)
        except OSError as exc:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="io_error",
                    path=args.path,
                    workspace=workspace,
                    message=f"Failed to create directory '{args.path}': {exc}",
                ),
                metadata={"error": "mkdir_failed"},
                exit_code=1,
            )

        mutation = FsMutationResult(
            path=args.path,
            action="created",
            message=f"Created directory {args.path}",
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


