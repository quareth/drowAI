from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from ..base_tool import BaseTool
from ..schemas import ToolResult
from .contracts import FsMutationResult, FsWriteArgs
from ._error_helpers import build_llm_error
from ._helpers import (
    build_tool_result,
    build_write_command,
    create_output_artifact,
    resolve_workspace_path_safe,
    should_create_artifact,
    workspace_root,
)
from ._reliability import (
    atomic_write_text,
    create_backup,
)

logger = logging.getLogger(__name__)


class FsWriteTool(BaseTool):
    """
    Write content to workspace file.
    
    The transport parameter controls execution routing (handled by executor):
    - "file-comm": Execute via Kali container queue
    - "pty": Execute `cat > {path} << 'EOF'\\n{content}\\nEOF` in PTY session
    - None: Executor auto-selects based on availability
    
    PTY routing is handled by the executor layer.
    
    PTY Use Cases:
        - Debugging: "Did the write succeed?"
        - Verification: User wants to see what's being written (audit trail)
        - Troubleshooting: Agent needs to diagnose write permission issues
    
    Security Guardrails:
        - All paths validated with resolve_workspace_path_safe()
        - Workspace isolation enforced
        - Content size validated before write
        - Overwrite protection via 'safe' mode
    
    PTY Command Equivalent:
        cat > /workspace/task_{task_id}/{path} << 'EOF'
        {content}
        EOF
    
    Examples:
        {"path": "config.yaml", "content": "key: value"}  # Auto-select
        {"path": "script.sh", "content": "#!/bin/bash\\necho test", "transport": "pty"}  # PTY for visibility
    """

    args_model = FsWriteArgs

    def build_command(self, args: FsWriteArgs) -> List[str]:
        """Build a PTY-safe command for writing file content."""

        workspace = workspace_root()
        target = resolve_workspace_path_safe(args.path, workspace=workspace)
        return build_write_command(
            path=str(target),
            content=args.content,
            create_parents=args.create_parents,
            append=False,
        )

    def parse_output(
        self,
        stdout: str,
        stderr: str,
        exit_code: int,
        args: FsWriteArgs,
    ) -> Dict[str, object]:
        """Parse write output into structured metadata."""

        bytes_written = len(args.content.encode(args.encoding, errors="replace"))
        action = "updated" if exit_code == 0 else "metadata"
        message = (
            f"Wrote {bytes_written} bytes to {args.path}"
            if exit_code == 0
            else f"Failed to write {args.path}"
        )
        mutation = FsMutationResult(
            path=args.path,
            action=action,
            bytes_changed=bytes_written if exit_code == 0 else None,
            message=message,
            extra={"exit_code": exit_code, "stderr": stderr} if exit_code != 0 else {},
        )
        return {"fs_write": mutation.model_dump()}

    def create_artifacts(
        self,
        stdout: str,
        args: FsWriteArgs,
        timestamp: Optional[int] = None,
        stderr: str = "",
    ) -> List[str]:
        """Persist write diagnostics as artifacts when needed."""

        created: List[str] = []
        if stdout and should_create_artifact(stdout):
            created.append(create_output_artifact(stdout, "fs_write", timestamp))
        if stderr:
            created.append(create_output_artifact(stderr, "fs_write_errors", timestamp))
        return created

    def run(self, args: FsWriteArgs) -> ToolResult:
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

        if not target.parent.exists():
            if args.create_parents:
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
            else:
                return build_tool_result(
                    success=False,
                    start=start,
                    stderr=build_llm_error(
                        error_type="missing_parent",
                        path=args.path,
                        workspace=workspace,
                        message=f"Parent directory for '{args.path}' does not exist.",
                    ),
                    metadata={"error": "missing_parent"},
                    exit_code=1,
                )

        bytes_written = len(args.content.encode(args.encoding, errors="replace"))

        existed_before = target.exists()

        if existed_before and args.overwrite == "safe":
            try:
                with target.open("rb") as handle:
                    existing_content = handle.read()
            except OSError:
                existing_content = b""
            if existing_content and existing_content != args.content.encode(args.encoding, errors="replace"):
                return build_tool_result(
                    success=False,
                    start=start,
                    stderr=build_llm_error(
                        error_type="would_overwrite",
                        path=args.path,
                        workspace=workspace,
                        message="File already exists and differs from new content.",
                        context={"existing_size": len(existing_content)},
                    ),
                    metadata={"error": "would_overwrite"},
                    exit_code=1,
                )

        # Create backup if requested and file exists
        backup_path: Optional[Path] = None
        if args.backup and existed_before:
            try:
                backup_path = create_backup(target)
                logger.debug(f"Created backup: {backup_path}")
            except OSError as exc:
                return build_tool_result(
                    success=False,
                    start=start,
                    stderr=build_llm_error(
                        error_type="io_error",
                        path=args.path,
                        workspace=workspace,
                        message=f"Failed to create backup for '{args.path}': {exc}",
                    ),
                    metadata={"error": "backup_failed"},
                    exit_code=1,
                )

        # Write content - atomic or standard
        try:
            if args.atomic:
                # Atomic write: temp file + rename pattern
                atomic_write_text(target, args.content, args.encoding)
            else:
                # Standard write (faster but not crash-safe)
                with target.open("w", encoding=args.encoding, newline="") as handle:
                    handle.write(args.content)
        except OSError as exc:
            return build_tool_result(
                success=False,
                start=start,
                stderr=build_llm_error(
                    error_type="write_error",
                    path=args.path,
                    workspace=workspace,
                    message=f"Failed to write '{args.path}': {exc}",
                ),
                metadata={"error": "io_error"},
                exit_code=1,
            )

        # Update mtime
        os.utime(target, None)

        # Build result message with backup info
        message_parts = [f"Wrote {bytes_written} bytes to {args.path}"]
        if backup_path:
            message_parts.append(f"(backup at {backup_path.name})")
        if args.atomic:
            message_parts.append("[atomic]")
        
        mutation = FsMutationResult(
            path=args.path,
            action="updated" if existed_before else "created",
            bytes_changed=bytes_written,
            message=" ".join(message_parts),
            extra={
                "backup_created": backup_path is not None,
                "backup_path": str(backup_path) if backup_path else None,
                "atomic_write": args.atomic,
            },
        )

        stdout = mutation.message or "File write completed"
        # Use the mutation object directly to preserve extra metadata
        metadata = {"fs_write": mutation.model_dump()}
        artifacts = self.create_artifacts(stdout, args)
        return build_tool_result(
            success=True,
            start=start,
            stdout=stdout,
            metadata=metadata,
            artifacts=artifacts,
        )


