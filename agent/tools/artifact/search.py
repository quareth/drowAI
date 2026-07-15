"""Thin adapter for task-scoped `artifact.search` runtime execution."""

from __future__ import annotations

import time

from ..base_tool import BaseTool
from ..schemas import ToolResult
from ._helpers import artifact_memory_session, build_search_stdout, resolve_active_task_id
from .contracts import ArtifactSearchArgs


class ArtifactSearchTool(BaseTool):
    """Search task-scoped artifact metadata using model-safe filters."""

    args_model = ArtifactSearchArgs

    def run(self, args: ArtifactSearchArgs) -> ToolResult:
        start = time.time()
        task_id = resolve_active_task_id()
        if task_id is None:
            return ToolResult(
                success=False,
                exit_code=2,
                stdout="",
                stderr=(
                    "artifact.search requires active runtime task context. "
                    "No task_id was provided by the executor runtime."
                ),
                artifacts=[],
                metadata={
                    "artifact_search": {
                        "status": "missing_runtime_task_context",
                        "requested_filters": args.model_dump(),
                    }
                },
                execution_time=max(0.0, time.time() - start),
            )

        try:
            from backend.services.artifact.memory_service import (
                ArtifactMemoryService,
                ArtifactSearchFilters,
            )

            filters = ArtifactSearchFilters(
                query=args.query,
                tool_name=args.tool_name,
                artifact_kind=args.artifact_kind,
                execution_id=args.execution_id,
                turn_id=args.turn_id,
                conversation_id=args.conversation_id,
                limit=args.limit,
                offset=args.offset,
            )
            with artifact_memory_session() as memory_service:
                page = memory_service.search_task_artifacts(task_id=task_id, filters=filters)
                payload = ArtifactMemoryService.catalog_page_to_dict(page)
        except Exception as exc:
            return ToolResult(
                success=False,
                exit_code=1,
                stdout="",
                stderr=f"artifact.search failed: {exc}",
                artifacts=[],
                metadata={
                    "artifact_search": {
                        "status": "error",
                        "requested_filters": args.model_dump(),
                    }
                },
                execution_time=max(0.0, time.time() - start),
            )

        return ToolResult(
            success=True,
            exit_code=0,
            stdout=build_search_stdout(payload),
            stderr="",
            artifacts=[],
            metadata={
                "artifact_search": {
                    "status": "ok",
                    "requested_filters": args.model_dump(),
                    "catalog_page": payload,
                }
            },
            execution_time=max(0.0, time.time() - start),
        )
