"""Thin adapter for task-scoped `artifact.read` runtime execution."""

from __future__ import annotations

import time
from dataclasses import asdict

from ..base_tool import BaseTool
from ..schemas import ToolResult
from ._helpers import artifact_memory_session, resolve_active_task_id
from .contracts import ArtifactReadArgs


class ArtifactReadTool(BaseTool):
    """Read one artifact by identifier using excerpt-first defaults."""

    args_model = ArtifactReadArgs

    def run(self, args: ArtifactReadArgs) -> ToolResult:
        start = time.time()
        task_id = resolve_active_task_id()
        if task_id is None:
            return ToolResult(
                success=False,
                exit_code=2,
                stdout="",
                stderr=(
                    "artifact.read requires active runtime task context. "
                    "No task_id was provided by the executor runtime."
                ),
                artifacts=[],
                metadata={
                    "artifact_read": {
                        "status": "missing_runtime_task_context",
                        "requested_read": args.model_dump(),
                    }
                },
                execution_time=max(0.0, time.time() - start),
            )

        try:
            from backend.services.artifact.memory_service import ArtifactReadRequest

            request = ArtifactReadRequest(
                mode=args.mode,
                query=args.query,
                max_chars=args.max_chars,
            )
            with artifact_memory_session() as memory_service:
                result = memory_service.read_task_artifact(
                    task_id=task_id,
                    artifact_id=args.artifact_id,
                    request=request,
                )
        except Exception as exc:
            return ToolResult(
                success=False,
                exit_code=1,
                stdout="",
                stderr=f"artifact.read failed: {exc}",
                artifacts=[],
                metadata={
                    "artifact_read": {
                        "status": "error",
                        "requested_read": args.model_dump(),
                    }
                },
                execution_time=max(0.0, time.time() - start),
            )

        status = str(result.status)
        read_succeeded = status in {"ready", "omitted_by_policy"}
        stderr = ""
        if status == "not_found":
            stderr = "Artifact not found in the active task scope."
        elif status == "not_available":
            stderr = "Artifact content is not currently available for read."
        elif status == "omitted_by_policy":
            stderr = "Full artifact content was omitted by policy; returning bounded content."

        return ToolResult(
            success=read_succeeded,
            exit_code=0 if read_succeeded else 1,
            stdout=str(result.content or ""),
            stderr=stderr,
            artifacts=[],
            metadata={
                "artifact_read": {
                    "status": status,
                    "artifact_id": str(result.artifact_id),
                    "mode_used": str(result.mode_used),
                    "truncated": bool(result.truncated),
                    "source": str(result.source),
                    "content_availability": str(result.content_availability),
                    "artifact": asdict(result.artifact) if result.artifact is not None else None,
                    "requested_read": args.model_dump(),
                }
            },
            execution_time=max(0.0, time.time() - start),
        )
