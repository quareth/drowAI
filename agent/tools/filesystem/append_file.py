from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .contracts import FsAppendArgs, FsMutationResult
from ._error_helpers import build_llm_error
from ._helpers import (
    build_tool_result,
    build_write_command,
    create_output_artifact,
    resolve_workspace_path_safe,
    should_create_artifact,
    workspace_root,
)


class FsAppendTool(BaseTool):
    """
    Append content to workspace file.
    
    The transport parameter controls execution routing (handled by executor):
    - "file-comm": Execute via Kali container queue
    - "pty": Execute `cat >> {path} << 'EOF'\\n{content}\\nEOF` in PTY session
    - None: Executor auto-selects
    
    PTY Use Cases:
        - Debugging: "Did the append succeed?"
        - Verification: User wants to see what's being appended
        - Troubleshooting: Diagnose append/permission issues
    
    Security Guardrails:
        - Workspace isolation enforced
        - Content size validated
    
    PTY Command Equivalent:
        cat >> /workspace/task_{task_id}/{path} << 'EOF'
        {content}
        EOF
    """

    args_model = FsAppendArgs

    def build_command(self, args: FsAppendArgs) -> List[str]:
        """Build a PTY-safe command for appending file content."""

        workspace = workspace_root()
        target = resolve_workspace_path_safe(args.path, workspace=workspace)
        return build_write_command(
            path=str(target),
            content=args.content,
            create_parents=args.create_if_missing,
            append=True,
        )

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FsAppendArgs,
    ) -> Dict[str, object]:
        """Parse append output into structured metadata."""

        bytes_appended = len(args.content.encode(args.encoding, errors="replace"))
        message = (
            f"Appended {bytes_appended} bytes to {args.path}"
            if exit_code == 0
            else f"Failed to append to {args.path}"
        )
        mutation = FsMutationResult(
            path=args.path,
            action="appended",
            bytes_changed=bytes_appended if exit_code == 0 else None,
            message=message,
            extra={"exit_code": exit_code, "stderr": stderr} if exit_code != 0 else {},
        )
        return {"fs_append": mutation.model_dump()}

    def create_artifacts(
        self,
        stdout: str,
        args: FsAppendArgs,
        timestamp: Optional[int] = None,
        stderr: str = "",
    ) -> List[str]:
        """Persist append diagnostics as artifacts when needed."""

        created: List[str] = []
        if stdout and should_create_artifact(stdout):
            created.append(create_output_artifact(stdout, "fs_append", timestamp))
        if stderr:
            created.append(create_output_artifact(stderr, "fs_append_errors", timestamp))
        return created

    def run(self, args: FsAppendArgs) -> ToolResult:
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
            if not args.create_if_missing:
                return build_tool_result(
                    success=False,
                    start=start,
                    stderr=build_llm_error(
                        error_type="not_found",
                        path=args.path,
                        workspace=workspace,
                        message=f"File '{args.path}' does not exist. Set create_if_missing=true to create it.",
                    ),
                    metadata={"error": "not_found"},
                    exit_code=1,
                )
            if not target.parent.exists():
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    return build_tool_result(
                        success=False,
                        start=start,
                        stderr=build_llm_error(
                            error_type="io_error",
                            path=args.path,
                            workspace=workspace,
                            message=f"Failed to create parent directories for '{args.path}': {exc}",
                        ),
                        metadata={"error": "mkdir_failed"},
                        exit_code=1,
                    )

        try:
            with target.open("a", encoding=args.encoding, newline="") as handle:
                handle.write(args.content)
        except OSError as exc:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="write_error",
                    path=args.path,
                    workspace=workspace,
                    message=f"Failed to append to '{args.path}': {exc}",
                ),
                metadata={"error": "io_error"},
                exit_code=1,
            )

        bytes_appended = len(args.content.encode(args.encoding, errors="replace"))
        mutation = FsMutationResult(
            path=args.path,
            action="appended",
            bytes_changed=bytes_appended,
            message=f"Appended {bytes_appended} bytes to {args.path}",
        )

        stdout = mutation.message or "Append completed"
        metadata = self.parse_output(stdout, "", 0, args)
        artifacts = self.create_artifacts(stdout, args)
        return build_tool_result(
            success=True,
            start=start,
            stdout=stdout,
            metadata=metadata,
            artifacts=artifacts,
        )


