"""Tool execution cancellation projection for chat stop.

This module keeps chat-cancel orchestration separate from provenance storage.
It marks active tool executions for a stopped turn as cancel-requested and
dispatches best-effort runtime cancellation through the provider boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.time_utils import utc_now
from backend.models.provenance import ToolExecution
from backend.repositories.tool_execution_repository import ToolExecutionRepository
from backend.services.runtime_provider.contracts import RuntimeActorType
from backend.services.runtime_provider.operations import RuntimeOperationService

_TERMINAL_TOOL_STATUSES = frozenset(
    {
        "completed",
        "succeeded",
        "success",
        "failed",
        "error",
        "timeout",
        "timed_out",
        "cancelled",
        "canceled",
        "denied",
    }
)


@dataclass(frozen=True)
class ToolCancelProjectionResult:
    """Summary of tool execution rows touched by a chat stop request."""

    marked_count: int
    execution_ids: tuple[str, ...]
    tool_call_ids: tuple[str, ...]
    command_ids: tuple[str, ...]
    runtime_job_ids: tuple[str, ...]
    process_state: str
    runtime_kill_attempted: bool
    runtime_kill_supported: bool


class ChatToolCancelProjectionService:
    """Project chat stop intent onto active tool provenance rows."""

    def __init__(self, db: Session, *, repository: ToolExecutionRepository | None = None) -> None:
        self._db = db
        self._repository = repository or ToolExecutionRepository(db)

    async def mark_turn_cancel_requested(
        self,
        *,
        tenant_id: int,
        task_id: int,
        turn_id: str | None,
        reason: str,
    ) -> ToolCancelProjectionResult:
        """Mark active tool rows for a turn as cancel-requested."""
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return self._empty()

        normalized_reason = (reason or "explicit_cancel").strip() or "explicit_cancel"
        rows = self._repository.mark_cancel_requested_by_turn(
            tenant_id=int(tenant_id),
            task_id=int(task_id),
            turn_id=normalized_turn_id,
            reason=normalized_reason,
            requested_at=utc_now(),
        )
        target_rows = self._load_runtime_cancel_targets(
            tenant_id=int(tenant_id),
            task_id=int(task_id),
            turn_id=normalized_turn_id,
            newly_marked_rows=rows,
        )
        runtime_metadata = await self._dispatch_runtime_cancel(
            task_id=int(task_id),
            turn_id=normalized_turn_id,
            rows=target_rows,
            reason=normalized_reason,
        )
        self._apply_runtime_metadata(rows=target_rows, runtime_metadata=runtime_metadata)
        process_state = str(runtime_metadata.get("process_state") or "orphaned_until_terminal")
        return ToolCancelProjectionResult(
            marked_count=len(rows),
            execution_ids=self._strings(row.id for row in target_rows),
            tool_call_ids=self._strings(row.tool_call_id for row in target_rows),
            command_ids=self._strings(row.command_id for row in target_rows),
            runtime_job_ids=self._strings(row.runtime_job_id for row in target_rows),
            process_state=process_state,
            runtime_kill_attempted=bool(runtime_metadata.get("runtime_kill_attempted")),
            runtime_kill_supported=bool(runtime_metadata.get("runtime_kill_supported")),
        )

    @classmethod
    def empty_result(cls) -> ToolCancelProjectionResult:
        """Return the canonical no-op cancellation projection result."""
        return cls._empty()

    def _load_runtime_cancel_targets(
        self,
        *,
        tenant_id: int,
        task_id: int,
        turn_id: str,
        newly_marked_rows: Iterable[ToolExecution],
    ) -> list[ToolExecution]:
        marked_ids = {str(row.id) for row in newly_marked_rows}
        rows = list(
            self._db.execute(
                select(ToolExecution)
                .where(
                    ToolExecution.tenant_id == int(tenant_id),
                    ToolExecution.task_id == int(task_id),
                    ToolExecution.turn_id == turn_id,
                )
                .order_by(ToolExecution.created_at.asc(), ToolExecution.id.asc())
            )
            .scalars()
            .all()
        )
        targets: list[ToolExecution] = []
        for row in rows:
            if str(row.id) in marked_ids:
                targets.append(row)
                continue
            status = str(row.status or "").strip().lower()
            if status in _TERMINAL_TOOL_STATUSES:
                continue
            metadata = row.execution_metadata if isinstance(row.execution_metadata, dict) else {}
            cancellation = metadata.get("cancellation")
            if isinstance(cancellation, dict) and bool(cancellation.get("cancel_requested")):
                targets.append(row)
        return targets

    async def _dispatch_runtime_cancel(
        self,
        *,
        task_id: int,
        turn_id: str,
        rows: Iterable[ToolExecution],
        reason: str,
    ) -> dict[str, object]:
        row_list = list(rows)
        command_ids = self._strings(row.command_id for row in row_list)
        runtime_job_ids = self._strings(row.runtime_job_id for row in row_list)
        if not command_ids and not runtime_job_ids:
            return {
                "process_state": "cancel_requested",
                "runtime_kill_attempted": False,
                "runtime_kill_supported": False,
            }

        try:
            runtime_operations = RuntimeOperationService(self._db)
            context = runtime_operations.context_for_internal_task(
                task_id=task_id,
                actor_type=RuntimeActorType.SYSTEM,
                actor_id="chat_stop",
            )
            result = await runtime_operations.run_for_context(
                context=context,
                operation="cancel_tool_command",
                call=lambda provider, request: provider.cancel_tool_command(request),
                payload={
                    "turn_id": turn_id,
                    "reason": reason,
                    "source": "chat_stop",
                    "command_ids": list(command_ids),
                    "runtime_job_ids": list(runtime_job_ids),
                    "execution_ids": list(self._strings(row.id for row in row_list)),
                    "tool_call_ids": list(self._strings(row.tool_call_id for row in row_list)),
                    "commands": [
                        {
                            "command_id": str(row.command_id or "").strip(),
                            "execution_transport": str(row.execution_transport or "").strip(),
                        }
                        for row in row_list
                        if str(row.command_id or "").strip()
                    ],
                },
            )
        except Exception:
            return {
                "process_state": "orphaned_until_terminal",
                "runtime_kill_attempted": bool(command_ids),
                "runtime_kill_supported": False,
            }

        metadata = dict(result.metadata or {})
        metadata.setdefault("process_state", "orphaned_until_terminal")
        metadata.setdefault("runtime_kill_attempted", bool(command_ids))
        metadata.setdefault("runtime_kill_supported", False)
        return metadata

    def _apply_runtime_metadata(
        self,
        *,
        rows: Iterable[ToolExecution],
        runtime_metadata: dict[str, object],
    ) -> None:
        row_list = list(rows)
        if not row_list:
            return
        cancellation_patch = {
            key: runtime_metadata[key]
            for key in (
                "process_state",
                "runtime_kill_attempted",
                "runtime_kill_supported",
                "cancellation_transport",
            )
            if key in runtime_metadata
        }
        if not cancellation_patch:
            return
        for row in row_list:
            metadata = row.execution_metadata if isinstance(row.execution_metadata, dict) else {}
            row_patch = dict(cancellation_patch)
            supported_command_ids = self._string_set(runtime_metadata.get("supported_command_ids"))
            unsupported_command_ids = self._string_set(runtime_metadata.get("unsupported_command_ids"))
            row_command_id = str(row.command_id or "").strip()
            if supported_command_ids or unsupported_command_ids:
                row_supported = bool(row_command_id and row_command_id in supported_command_ids)
                row_patch["runtime_kill_supported"] = row_supported
                row_patch["runtime_kill_attempted"] = row_supported
                if not row_supported:
                    row_patch["process_state"] = "orphaned_until_terminal"
            row.execution_metadata = self._merge_json_dicts(
                metadata,
                {"cancellation": row_patch},
            )
        self._db.flush()

    @staticmethod
    def _strings(values: Iterable[object | None]) -> tuple[str, ...]:
        return tuple(str(value) for value in values if value is not None and str(value).strip())

    @staticmethod
    def _string_set(value: object) -> set[str]:
        if isinstance(value, (list, tuple, set)):
            return {str(item) for item in value if str(item).strip()}
        return set()

    @staticmethod
    def _empty() -> ToolCancelProjectionResult:
        return ToolCancelProjectionResult(
            marked_count=0,
            execution_ids=(),
            tool_call_ids=(),
            command_ids=(),
            runtime_job_ids=(),
            process_state="none",
            runtime_kill_attempted=False,
            runtime_kill_supported=False,
        )

    @staticmethod
    def _merge_json_dicts(base: dict, patch: dict) -> dict:
        merged = dict(base)
        for key, value in patch.items():
            current = merged.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                merged[key] = ChatToolCancelProjectionService._merge_json_dicts(current, value)
            else:
                merged[key] = value
        return merged


__all__ = ["ChatToolCancelProjectionService", "ToolCancelProjectionResult"]
