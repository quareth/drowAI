"""
Data access for tool execution provenance records.

This module provides CRUD-style operations for the `tool_executions` table.
The repository performs flushes but leaves transaction boundaries to callers.
"""

from __future__ import annotations

from datetime import datetime
from copy import deepcopy
from typing import Any, Dict, List, Optional
import uuid

from sqlalchemy.orm import Session
from sqlalchemy import select

from backend.models.core import Task
from backend.models.provenance import ToolExecution


class ToolExecutionRepository:
    """Repository for `ToolExecution` persistence and filtering queries."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def create(
        self,
        *,
        task_id: int,
        tenant_id: int | None = None,
        runtime_job_id: uuid.UUID | None = None,
        runner_id: uuid.UUID | None = None,
        execution_site_id: uuid.UUID | None = None,
        command_id: str | None = None,
        workspace_id: str | None = None,
        tool_name: str,
        tool_arguments: Dict[str, Any],
        agent_path: str,
        status: str,
        started_at: datetime,
        chat_message_id: Optional[int] = None,
        tool_call_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        turn_id: Optional[str] = None,
        turn_sequence: Optional[int] = None,
        purpose: Optional[str] = None,
        execution_transport: Optional[str] = None,
        workspace_path: Optional[str] = None,
        container_path: Optional[str] = None,
        exit_code: Optional[int] = None,
        finished_at: Optional[datetime] = None,
        duration_ms: Optional[int] = None,
        execution_metadata: Optional[Dict[str, Any]] = None,
    ) -> ToolExecution:
        """Insert one tool execution row and return the ORM object."""
        resolved_tenant_id = self._resolve_tenant_id(task_id=task_id, tenant_id=tenant_id)
        execution = ToolExecution(
            tenant_id=resolved_tenant_id,
            task_id=task_id,
            runtime_job_id=runtime_job_id,
            runner_id=runner_id,
            execution_site_id=execution_site_id,
            command_id=command_id,
            workspace_id=workspace_id,
            chat_message_id=chat_message_id,
            tool_call_id=tool_call_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            turn_sequence=turn_sequence,
            tool_name=tool_name,
            tool_arguments=tool_arguments,
            purpose=purpose,
            agent_path=agent_path,
            execution_transport=execution_transport,
            workspace_path=workspace_path,
            container_path=container_path,
            status=status,
            exit_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=duration_ms,
            execution_metadata=execution_metadata or {},
        )
        self.db.add(execution)
        self.db.flush()
        self.db.refresh(execution)
        return execution

    def get_by_runtime_binding(
        self,
        *,
        tenant_id: int,
        task_id: int,
        runtime_job_id: str | uuid.UUID,
        command_id: str | None = None,
    ) -> Optional[ToolExecution]:
        """Return one execution bound to tenant/task/runtime job identity."""
        parsed_runtime_job_id = self._parse_uuid(runtime_job_id)
        if parsed_runtime_job_id is None:
            return None

        query = self.db.query(ToolExecution).filter(
            ToolExecution.tenant_id == int(tenant_id),
            ToolExecution.task_id == int(task_id),
            ToolExecution.runtime_job_id == parsed_runtime_job_id,
        )
        if command_id is not None:
            query = query.filter(ToolExecution.command_id == str(command_id))
        return query.order_by(ToolExecution.created_at.asc()).first()

    def get_by_task_tool_call_id(
        self,
        *,
        tenant_id: int,
        task_id: int,
        tool_call_id: str | None,
    ) -> Optional[ToolExecution]:
        """Return one execution by tenant/task/tool-call identity."""
        normalized_tool_call_id = str(tool_call_id or "").strip()
        if not normalized_tool_call_id:
            return None
        return (
            self.db.query(ToolExecution)
            .filter(
                ToolExecution.tenant_id == int(tenant_id),
                ToolExecution.task_id == int(task_id),
                ToolExecution.tool_call_id == normalized_tool_call_id,
            )
            .order_by(ToolExecution.created_at.asc())
            .first()
        )

    def _resolve_tenant_id(self, *, task_id: int, tenant_id: int | None) -> int:
        """Resolve tenant ownership for new tool execution writes."""
        if tenant_id is not None:
            return int(tenant_id)

        resolved = self.db.execute(
            select(Task.tenant_id).where(Task.id == int(task_id))
        ).scalar_one_or_none()
        if resolved is None:
            raise ValueError(f"Cannot resolve tenant_id for task_id={task_id}")
        return int(resolved)

    def get_by_id(self, execution_id: str | uuid.UUID) -> Optional[ToolExecution]:
        """Return execution by primary key UUID."""
        parsed_id = self._parse_uuid(execution_id)
        if parsed_id is None:
            return None
        return self.db.get(ToolExecution, parsed_id)

    def get_by_tenant_task_execution_id(
        self,
        *,
        tenant_id: int,
        task_id: int,
        execution_id: str | uuid.UUID,
    ) -> Optional[ToolExecution]:
        """Return one execution constrained by tenant/task/execution identity."""
        parsed_id = self._parse_uuid(execution_id)
        if parsed_id is None:
            return None
        return (
            self.db.query(ToolExecution)
            .filter(
                ToolExecution.tenant_id == int(tenant_id),
                ToolExecution.task_id == int(task_id),
                ToolExecution.id == parsed_id,
            )
            .one_or_none()
        )

    def get_by_tool_call_id(self, *, task_id: int, tool_call_id: str) -> Optional[ToolExecution]:
        """Return execution by task-scoped tool_call_id."""
        return (
            self.db.query(ToolExecution)
            .filter(
                ToolExecution.task_id == task_id,
                ToolExecution.tool_call_id == tool_call_id,
            )
            .one_or_none()
        )

    def update_status(
        self,
        *,
        execution_id: str | uuid.UUID,
        status: str,
        exit_code: Optional[int] = None,
        finished_at: Optional[datetime] = None,
        duration_ms: Optional[int] = None,
        execution_metadata_patch: Optional[Dict[str, Any]] = None,
    ) -> Optional[ToolExecution]:
        """Update mutable status fields for a tool execution."""
        execution = self.get_by_id(execution_id)
        if execution is None:
            return None

        execution.status = status
        if exit_code is not None:
            execution.exit_code = exit_code
        if finished_at is not None:
            execution.finished_at = finished_at
        if duration_ms is not None:
            execution.duration_ms = duration_ms
        if execution_metadata_patch:
            execution.execution_metadata = self._merge_json_dicts(
                execution.execution_metadata,
                execution_metadata_patch,
            )

        self.db.flush()
        self.db.refresh(execution)
        return execution

    def update_status_by_tenant_scope(
        self,
        *,
        tenant_id: int,
        task_id: int,
        execution_id: str | uuid.UUID,
        status: str,
        exit_code: Optional[int] = None,
        finished_at: Optional[datetime] = None,
        duration_ms: Optional[int] = None,
        execution_metadata_patch: Optional[Dict[str, Any]] = None,
    ) -> Optional[ToolExecution]:
        """Update execution status constrained by tenant/task/execution identity."""
        execution = self.get_by_tenant_task_execution_id(
            tenant_id=tenant_id,
            task_id=task_id,
            execution_id=execution_id,
        )
        if execution is None:
            return None

        execution.status = status
        if exit_code is not None:
            execution.exit_code = exit_code
        if finished_at is not None:
            execution.finished_at = finished_at
        if duration_ms is not None:
            execution.duration_ms = duration_ms
        if execution_metadata_patch:
            execution.execution_metadata = self._merge_json_dicts(
                execution.execution_metadata,
                execution_metadata_patch,
            )

        self.db.flush()
        self.db.refresh(execution)
        return execution

    def mark_cancel_requested_by_turn(
        self,
        *,
        tenant_id: int,
        task_id: int,
        turn_id: str,
        reason: str,
        requested_at: datetime,
    ) -> List[ToolExecution]:
        """Mark non-terminal executions in a turn as cancel-requested."""
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_turn_id:
            return []

        terminal_statuses = {
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
        rows = (
            self.db.query(ToolExecution)
            .filter(
                ToolExecution.tenant_id == int(tenant_id),
                ToolExecution.task_id == int(task_id),
                ToolExecution.turn_id == normalized_turn_id,
            )
            .order_by(ToolExecution.created_at.asc())
            .all()
        )

        updated: List[ToolExecution] = []
        for execution in rows:
            status = str(execution.status or "").strip().lower()
            metadata = execution.execution_metadata if isinstance(execution.execution_metadata, dict) else {}
            cancellation = metadata.get("cancellation")
            already_requested = isinstance(cancellation, dict) and bool(cancellation.get("cancel_requested"))
            if already_requested:
                continue
            if status in terminal_statuses:
                continue
            execution.status = "cancel_requested"
            execution.execution_metadata = self._merge_json_dicts(
                metadata,
                {
                    "cancellation": {
                        "cancel_requested": True,
                        "reason": reason,
                        "requested_at": requested_at.isoformat(),
                        "process_state": "orphaned_until_terminal",
                        "runtime_kill_attempted": False,
                        "runtime_kill_supported": False,
                    }
                },
            )
            updated.append(execution)

        if updated:
            self.db.flush()
            for execution in updated:
                self.db.refresh(execution)
        return updated

    def get_by_task(self, *, task_id: int, limit: int = 100, offset: int = 0) -> List[ToolExecution]:
        """Return executions for a task ordered newest-first."""
        return (
            self.db.query(ToolExecution)
            .filter(ToolExecution.task_id == task_id)
            .order_by(ToolExecution.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def get_by_conversation_turn(
        self,
        *,
        task_id: int,
        conversation_id: str,
        turn_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ToolExecution]:
        """Return executions for a task conversation, optionally narrowed by turn."""
        query = self.db.query(ToolExecution).filter(
            ToolExecution.task_id == task_id,
            ToolExecution.conversation_id == conversation_id,
        )
        if turn_id is not None:
            query = query.filter(ToolExecution.turn_id == turn_id)
        return query.order_by(ToolExecution.created_at.desc()).offset(offset).limit(limit).all()

    def get_by_tool_name(
        self,
        *,
        task_id: int,
        tool_name: str,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ToolExecution]:
        """Return executions for a task filtered by tool name."""
        return (
            self.db.query(ToolExecution)
            .filter(
                ToolExecution.task_id == task_id,
                ToolExecution.tool_name == tool_name,
            )
            .order_by(ToolExecution.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    def get_by_time_range(
        self,
        *,
        task_id: int,
        start_time: datetime,
        end_time: datetime,
        limit: int = 100,
        offset: int = 0,
    ) -> List[ToolExecution]:
        """Return executions for a task between start/end timestamps."""
        return (
            self.db.query(ToolExecution)
            .filter(
                ToolExecution.task_id == task_id,
                ToolExecution.started_at >= start_time,
                ToolExecution.started_at <= end_time,
            )
            .order_by(ToolExecution.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

    @staticmethod
    def _parse_uuid(value: str | uuid.UUID) -> Optional[uuid.UUID]:
        if isinstance(value, uuid.UUID):
            return value
        try:
            return uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            return None

    @staticmethod
    def _merge_json_dicts(
        base: Optional[Dict[str, Any]],
        patch: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Recursively merge JSON dictionaries without dropping existing keys."""
        merged: Dict[str, Any] = deepcopy(base) if isinstance(base, dict) else {}
        for key, value in patch.items():
            current = merged.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                merged[key] = ToolExecutionRepository._merge_json_dicts(current, value)
            else:
                merged[key] = deepcopy(value)
        return merged
