from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .contracts import FsCopyArgs, FsMutationResult
from ._error_helpers import build_llm_error
from ._helpers import (
    build_tool_result,
    build_transfer_command,
    create_output_artifact,
    resolve_workspace_path_safe,
    should_create_artifact,
    to_workspace_relative,
    workspace_root,
)


class FsCopyTool(BaseTool):
    """
    Copy files or directories in workspace.
    
    The transport parameter controls execution routing (handled by executor):
    - "file-comm": Execute via Kali container queue
    - "pty": Execute `cp -r {src} {dst}` in PTY session
    - None: Executor auto-selects
    
    PTY Use Cases:
        - Verification: User can verify copy operation
        - Troubleshooting: Diagnose copy/permission issues
    
    PTY Command Equivalent:
        cp -r /workspace/task_{task_id}/{src} /workspace/task_{task_id}/{dst}
    """

    args_model = FsCopyArgs

    def build_command(self, args: FsCopyArgs) -> List[str]:
        """Build a PTY-safe command for copying paths."""

        workspace = workspace_root()
        src = resolve_workspace_path_safe(args.src, workspace=workspace)
        dest = resolve_workspace_path_safe(args.dest, workspace=workspace)
        return build_transfer_command(
            operation="cp",
            src=str(src),
            dest=str(dest),
            recursive=src.is_dir(),
            create_parents=args.create_dest_parents,
        )

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FsCopyArgs,
    ) -> Dict[str, object]:
        """Parse copy output into structured metadata."""

        message = (
            f"Copied {args.src} -> {args.dest}" if exit_code == 0 else f"Failed to copy {args.src}"
        )
        mutation = FsMutationResult(
            path=args.dest,
            action="copied",
            message=message,
            extra={
                "source": args.src,
                "destination": args.dest,
                "exit_code": exit_code,
                "stderr": stderr,
            }
            if exit_code != 0
            else {"source": args.src, "destination": args.dest},
        )
        return {"fs_copy": mutation.model_dump()}

    def create_artifacts(
        self,
        stdout: str,
        args: FsCopyArgs,
        timestamp: Optional[int] = None,
        stderr: str = "",
    ) -> List[str]:
        """Persist copy diagnostics as artifacts when needed."""

        created: List[str] = []
        if stdout and should_create_artifact(stdout):
            created.append(create_output_artifact(stdout, "fs_copy", timestamp))
        if stderr:
            created.append(create_output_artifact(stderr, "fs_copy_errors", timestamp))
        return created

    def run(self, args: FsCopyArgs) -> ToolResult:
        start = time.time()
        workspace = workspace_root()

        try:
            src: Path = resolve_workspace_path_safe(args.src, workspace=workspace)
            dest: Path = resolve_workspace_path_safe(args.dest, workspace=workspace)
        except ValueError as exc:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="path_out_of_workspace",
                    path=args.src,
                    workspace=workspace,
                    message=str(exc),
                ),
                metadata={"error": "path_out_of_workspace"},
            )

        if not src.exists():
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="source_not_found",
                    path=args.src,
                    workspace=workspace,
                    message=f"Source '{args.src}' does not exist.",
                ),
                metadata={"error": "not_found"},
                exit_code=1,
            )

        if dest.exists() and not args.overwrite:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="dest_exists",
                    path=args.src,
                    workspace=workspace,
                    message=f"Destination '{args.dest}' already exists.",
                    context={"dest": args.dest},
                ),
                metadata={"error": "destination_exists"},
                exit_code=1,
            )

        if not dest.parent.exists():
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return build_tool_result(
                    success=False,
                    start=start,
                    stderr=build_llm_error(
                        error_type="io_error",
                        path=args.dest,
                        workspace=workspace,
                        message=f"Failed to create destination directories: {exc}",
                    ),
                    metadata={"error": "mkdir_failed"},
                    exit_code=1,
                )

        try:
            if src.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(src, dest, dirs_exist_ok=args.overwrite)
            else:
                shutil.copy2(src, dest)
        except OSError as exc:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="io_error",
                    path=args.src,
                    workspace=workspace,
                    message=f"Failed to copy '{args.src}' to '{args.dest}': {exc}",
                ),
                metadata={"error": "io_error"},
                exit_code=1,
            )

        mutation = FsMutationResult(
            path=to_workspace_relative(dest, workspace),
            action="copied",
            message=f"Copied {args.src} -> {args.dest}",
            extra={"source": args.src, "destination": args.dest},
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


