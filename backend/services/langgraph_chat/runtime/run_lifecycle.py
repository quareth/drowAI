"""Runtime lifecycle mirror for interactive chat runs.

Durable turn state transitions live in ``TurnWorkflowService``. This service
keeps the in-memory run registry aligned for fast cancel checks, emits run
state packets, and records cancel-request metadata on existing active workflow
rows.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import threading
from typing import Dict, Iterable, Optional

from sqlalchemy import desc
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.core.time_utils import to_utc, utc_now
from backend.models.hitl import TurnWorkflow

from backend.services.langgraph_chat.runtime.run_registry import RunRecord, get_run_registry
from backend.services.langgraph_chat.streaming.status_events import emit_run_state_event
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import TurnWorkflowState

logger = logging.getLogger(__name__)

_TERMINAL_VISIBILITY_SECONDS = 90.0
_ACTIVE_STATES = {
    TurnWorkflowState.RUNNING.value,
    TurnWorkflowState.RESUMED.value,
    TurnWorkflowState.RETRYING.value,
    TurnWorkflowState.WAITING_FOR_HUMAN.value,
}
_TERMINAL_STATES = {
    TurnWorkflowState.COMPLETED.value,
    TurnWorkflowState.FAILED.value,
}


class RunLifecycleService:
    """Mirror run lifecycle state and route cancel requests."""

    def __init__(self) -> None:
        self._registry = get_run_registry()  # fallback-only compatibility path
        self._cancellable_lock = threading.RLock()
        self._cancellable_tasks: Dict[
            tuple[int, str], tuple[asyncio.AbstractEventLoop, asyncio.Task[object]]
        ] = {}

    def register_cancellable_task(
        self,
        *,
        task_id: int,
        turn_id: str,
        task: asyncio.Task[object],
    ) -> None:
        """Register the local async owner that an accepted Stop request cancels."""
        loop = asyncio.get_running_loop()
        with self._cancellable_lock:
            self._cancellable_tasks[(task_id, turn_id)] = (loop, task)

    def unregister_cancellable_task(
        self,
        *,
        task_id: int,
        turn_id: str,
        task: asyncio.Task[object],
    ) -> None:
        """Remove only the matching async owner after its cancellable scope ends."""
        with self._cancellable_lock:
            registered = self._cancellable_tasks.get((task_id, turn_id))
            if registered is not None and registered[1] is task:
                self._cancellable_tasks.pop((task_id, turn_id), None)

    def _cancel_registered_task(self, *, task_id: int, turn_id: Optional[str]) -> None:
        """Cancel the matching local async owner without crossing task identity."""
        if not turn_id:
            return
        with self._cancellable_lock:
            registered = self._cancellable_tasks.get((task_id, turn_id))
        if registered is None:
            return
        loop, task = registered
        if task.done():
            return
        loop.call_soon_threadsafe(task.cancel)

    @staticmethod
    def _utcnow() -> datetime:
        return utc_now()

    @staticmethod
    def _timestamp(value: Optional[datetime]) -> float:
        if value is None:
            return 0.0
        return to_utc(value).timestamp()

    @staticmethod
    def _metadata(row: TurnWorkflow) -> Dict[str, object]:
        raw = row.workflow_metadata
        return raw if isinstance(raw, dict) else {}

    def _map_state(self, row: TurnWorkflow) -> str:
        metadata = self._metadata(row)
        if row.state == TurnWorkflowState.WAITING_FOR_HUMAN.value:
            return "waiting_for_human"
        if row.state in {
            TurnWorkflowState.RUNNING.value,
            TurnWorkflowState.RESUMED.value,
            TurnWorkflowState.RETRYING.value,
        }:
            return "running"
        if row.state == TurnWorkflowState.COMPLETED.value:
            return "completed"
        if row.state == TurnWorkflowState.FAILED.value:
            terminal = metadata.get("terminal_status")
            if isinstance(terminal, str):
                normalized = terminal.strip().lower()
                if normalized in {"cancelled", "declined"}:
                    return normalized
            cancel_requested = bool(metadata.get("cancel_requested"))
            return "cancelled" if cancel_requested else "failed"
        terminal = metadata.get("terminal_status")
        if isinstance(terminal, str) and terminal.strip():
            normalized = terminal.strip().lower()
            if normalized in {
                "completed",
                "waiting_for_human",
                "cancelled",
                "declined",
                "failed",
                "running",
            }:
                return normalized
        return "unknown"

    def _to_record(self, row: TurnWorkflow) -> RunRecord:
        metadata = self._metadata(row)
        updated_at = row.updated_at or row.created_at
        ended_dt = row.completed_at or row.failed_at
        state = self._map_state(row)
        return RunRecord(
            task_id=row.task_id,
            turn_id=row.turn_id,
            conversation_id=row.conversation_id,
            state=state,
            cancel_requested=bool(metadata.get("cancel_requested")),
            cancel_reason=(metadata.get("cancel_reason") if isinstance(metadata.get("cancel_reason"), str) else None),
            started_at=self._timestamp(row.created_at),
            updated_at=self._timestamp(updated_at),
            ended_at=(self._timestamp(ended_dt) if ended_dt is not None else None),
        )

    def _terminal_visible(self, row: TurnWorkflow) -> bool:
        if row.state not in _TERMINAL_STATES:
            return False
        updated_at = row.updated_at or row.created_at
        if updated_at is None:
            return True
        updated_at = to_utc(updated_at)
        elapsed = (self._utcnow() - updated_at).total_seconds()
        return elapsed <= _TERMINAL_VISIBILITY_SECONDS

    @staticmethod
    def _set_cancel_metadata(row: TurnWorkflow, *, requested: bool, reason: Optional[str]) -> None:
        metadata = row.workflow_metadata if isinstance(row.workflow_metadata, dict) else {}
        metadata = dict(metadata)
        metadata["cancel_requested"] = bool(requested)
        if reason:
            metadata["cancel_reason"] = reason
        row.workflow_metadata = metadata

    def _project_end_status(self, row: TurnWorkflow, *, status: str) -> None:
        """Persist terminal run projection used by status APIs and stream events."""
        normalized = (status or "failed").strip().lower()
        metadata = dict(row.workflow_metadata if isinstance(row.workflow_metadata, dict) else {})
        if metadata.get("outcome_type") == "provider_refusal":
            row.state = TurnWorkflowState.FAILED.value
            row.failed_at = row.failed_at or self._utcnow()
            row.completed_at = None
            metadata["terminal_status"] = "declined"
            metadata["retryable"] = False
            row.workflow_metadata = metadata
            return
        existing_terminal = str(metadata.get("terminal_status") or "").strip().lower()
        if existing_terminal == "cancelled" and normalized != "cancelled":
            metadata["terminal_status"] = "cancelled"
            metadata["cancel_requested"] = True
            metadata.setdefault("cancel_reason", "explicit_cancel")
            row.workflow_metadata = metadata
            return
        if normalized == "completed":
            row.state = TurnWorkflowState.COMPLETED.value
            row.completed_at = row.completed_at or self._utcnow()
            metadata["terminal_status"] = "completed"
        elif normalized == "cancelled":
            row.state = TurnWorkflowState.FAILED.value
            row.failed_at = row.failed_at or self._utcnow()
            metadata["terminal_status"] = "cancelled"
            metadata["cancel_requested"] = True
            metadata.setdefault("cancel_reason", "explicit_cancel")
        elif normalized == "failed":
            row.state = TurnWorkflowState.FAILED.value
            row.failed_at = row.failed_at or self._utcnow()
            metadata["terminal_status"] = "failed"
        elif normalized == "waiting_for_human":
            row.state = TurnWorkflowState.WAITING_FOR_HUMAN.value
            metadata["terminal_status"] = "waiting_for_human"
        row.workflow_metadata = metadata

    def _open_session(self, db_session: Optional[Session]) -> tuple[Session, bool]:
        if db_session is not None:
            return db_session, False
        return SessionLocal(), True

    def _sync_registry_start(
        self,
        *,
        task_id: int,
        turn_id: str,
        conversation_id: Optional[str],
    ) -> None:
        """Best-effort in-process mirror used by fallback and fast cancel checks."""
        try:
            self._registry.start(
                task_id=task_id,
                turn_id=turn_id,
                conversation_id=conversation_id,
            )
        except Exception:
            logger.debug(
                "Failed to mirror lifecycle start to in-memory registry (task=%s turn_id=%s)",
                task_id,
                turn_id,
                exc_info=True,
            )

    def _sync_registry_cancel(
        self,
        *,
        task_id: int,
        turn_id: str,
        reason: Optional[str],
    ) -> None:
        """Best-effort in-process mirror for explicit cancel requests."""
        try:
            active = self._registry.get_active(task_id)
            if active is None or active.turn_id != turn_id:
                self._registry.start(task_id=task_id, turn_id=turn_id, conversation_id=None)
            self._registry.request_cancel(task_id=task_id, turn_id=turn_id, reason=reason)
        except Exception:
            logger.debug(
                "Failed to mirror lifecycle cancel to in-memory registry (task=%s turn_id=%s)",
                task_id,
                turn_id,
                exc_info=True,
            )

    def start_run(
        self,
        *,
        task_id: int,
        turn_id: str,
        conversation_id: Optional[str] = None,
        db_session: Optional[Session] = None,
    ) -> RunRecord:
        session, owns_session = self._open_session(db_session)
        try:
            row = (
                session.query(TurnWorkflow)
                .filter(TurnWorkflow.task_id == task_id, TurnWorkflow.turn_id == turn_id)
                .order_by(desc(TurnWorkflow.id))
                .first()
            )
            self._sync_registry_start(
                task_id=task_id,
                turn_id=turn_id,
                conversation_id=conversation_id,
            )
            if row is None:
                fallback = self._registry.get_active(task_id)
                if fallback is None or fallback.turn_id != turn_id:
                    fallback = self._registry.start(
                        task_id=task_id,
                        turn_id=turn_id,
                        conversation_id=conversation_id,
                    )
                emit_run_state_event(
                    task_id=task_id,
                    state=fallback.state,
                    turn_id=fallback.turn_id,
                    cancel_requested=fallback.cancel_requested,
                    cancel_reason=fallback.cancel_reason,
                    conversation_id=fallback.conversation_id,
                )
                return fallback
            record = self._to_record(row)
            if record.cancel_requested:
                self._sync_registry_cancel(
                    task_id=task_id,
                    turn_id=turn_id,
                    reason=record.cancel_reason,
                )
            emit_run_state_event(
                task_id=task_id,
                state=record.state,
                turn_id=record.turn_id,
                cancel_requested=record.cancel_requested,
                cancel_reason=record.cancel_reason,
                conversation_id=record.conversation_id,
            )
            return record
        except Exception:
            logger.warning(
                "Falling back to in-memory lifecycle start (task=%s turn_id=%s)",
                task_id,
                turn_id,
                exc_info=True,
            )
            fallback = self._registry.start(
                task_id=task_id,
                turn_id=turn_id,
                conversation_id=conversation_id,
            )
            emit_run_state_event(
                task_id=task_id,
                state=fallback.state,
                turn_id=fallback.turn_id,
                cancel_requested=fallback.cancel_requested,
                cancel_reason=fallback.cancel_reason,
                conversation_id=fallback.conversation_id,
            )
            return fallback
        finally:
            if owns_session:
                session.close()

    def get_active_run(self, task_id: int, *, db_session: Optional[Session] = None) -> Optional[RunRecord]:
        session, owns_session = self._open_session(db_session)
        try:
            rows = (
                session.query(TurnWorkflow)
                .filter(TurnWorkflow.task_id == task_id)
                .order_by(desc(TurnWorkflow.updated_at), desc(TurnWorkflow.id))
                .all()
            )
            active = next((row for row in rows if row.state in _ACTIVE_STATES), None)
            if active is not None:
                return self._to_record(active)
            latest = rows[0] if rows else None
            if latest is not None and self._terminal_visible(latest):
                return self._to_record(latest)
            return self._registry.get_active(task_id)
        except Exception:
            logger.debug(
                "Falling back to in-memory lifecycle read (task=%s)",
                task_id,
                exc_info=True,
            )
            return self._registry.get_active(task_id)
        finally:
            if owns_session:
                session.close()

    def get_runs_for_tasks(
        self,
        task_ids: Iterable[int],
        *,
        db_session: Optional[Session] = None,
    ) -> Dict[int, Optional[RunRecord]]:
        ids = [int(task_id) for task_id in task_ids if isinstance(task_id, int) and task_id > 0]
        if not ids:
            return {}
        session, owns_session = self._open_session(db_session)
        try:
            rows = (
                session.query(TurnWorkflow)
                .filter(TurnWorkflow.task_id.in_(ids))
                .order_by(TurnWorkflow.task_id.asc(), desc(TurnWorkflow.updated_at), desc(TurnWorkflow.id))
                .all()
            )
            grouped: Dict[int, list[TurnWorkflow]] = {}
            for row in rows:
                grouped.setdefault(row.task_id, []).append(row)
            resolved: Dict[int, Optional[RunRecord]] = {}
            for task_id in ids:
                candidates = grouped.get(task_id, [])
                active = next((row for row in candidates if row.state in _ACTIVE_STATES), None)
                if active is not None:
                    resolved[task_id] = self._to_record(active)
                    continue
                latest = candidates[0] if candidates else None
                if latest is not None and self._terminal_visible(latest):
                    resolved[task_id] = self._to_record(latest)
                else:
                    resolved[task_id] = self._registry.get_active(task_id)
            return resolved
        except Exception:
            logger.debug("Falling back to in-memory batched lifecycle read", exc_info=True)
            return {task_id: self._registry.get_active(task_id) for task_id in ids}
        finally:
            if owns_session:
                session.close()

    def request_cancel(
        self,
        *,
        task_id: int,
        turn_id: Optional[str] = None,
        reason: Optional[str] = None,
        db_session: Optional[Session] = None,
    ) -> dict:
        session, owns_session = self._open_session(db_session)
        try:
            row = (
                session.query(TurnWorkflow)
                .filter(
                    TurnWorkflow.task_id == task_id,
                    TurnWorkflow.state.in_(list(_ACTIVE_STATES)),
                )
                .order_by(desc(TurnWorkflow.updated_at), desc(TurnWorkflow.id))
                .first()
            )
            if row is None:
                if turn_id:
                    terminal_row = (
                        session.query(TurnWorkflow)
                        .filter(TurnWorkflow.task_id == task_id, TurnWorkflow.turn_id == turn_id)
                        .order_by(desc(TurnWorkflow.updated_at), desc(TurnWorkflow.id))
                        .first()
                    )
                    if terminal_row is not None and self._map_state(terminal_row) == "cancelled":
                        record = self._to_record(terminal_row)
                        emit_run_state_event(
                            task_id=task_id,
                            state=record.state,
                            turn_id=record.turn_id,
                            cancel_requested=record.cancel_requested,
                            cancel_reason=record.cancel_reason,
                            conversation_id=record.conversation_id,
                        )
                        return {
                            "cancelled": False,
                            "already_cancelled": True,
                            "active": False,
                            "turn_id": terminal_row.turn_id,
                            "cancel_reason": record.cancel_reason,
                        }
                fallback = self._registry.request_cancel(
                    task_id=task_id,
                    turn_id=turn_id,
                    reason=reason,
                )
                if fallback.get("active") or fallback.get("cancelled") or fallback.get("already_cancelled"):
                    if fallback.get("cancelled"):
                        self._cancel_registered_task(
                            task_id=task_id,
                            turn_id=str(fallback.get("turn_id") or turn_id or "") or None,
                        )
                    return fallback
                return {
                    "cancelled": False,
                    "already_cancelled": False,
                    "active": False,
                    "turn_id": turn_id,
                    "reason": "not_running",
                }
            if turn_id and row.turn_id != turn_id:
                return {
                    "cancelled": False,
                    "already_cancelled": False,
                    "active": True,
                    "turn_id": row.turn_id,
                    "reason": "turn_id_mismatch",
                }
            metadata = self._metadata(row)
            if bool(metadata.get("cancel_requested")):
                self._sync_registry_cancel(
                    task_id=task_id,
                    turn_id=row.turn_id,
                    reason=(metadata.get("cancel_reason") if isinstance(metadata.get("cancel_reason"), str) else None),
                )
                record = self._to_record(row)
                emit_run_state_event(
                    task_id=task_id,
                    state=record.state,
                    turn_id=record.turn_id,
                    cancel_requested=record.cancel_requested,
                    cancel_reason=record.cancel_reason,
                    conversation_id=record.conversation_id,
                )
                return {
                    "cancelled": False,
                    "already_cancelled": True,
                    "active": True,
                    "turn_id": row.turn_id,
                }
            cancel_reason = (reason or "explicit_cancel").strip() or "explicit_cancel"
            self._set_cancel_metadata(row, requested=True, reason=cancel_reason)
            session.commit()
            self._sync_registry_cancel(
                task_id=task_id,
                turn_id=row.turn_id,
                reason=cancel_reason,
            )
            self._cancel_registered_task(task_id=task_id, turn_id=row.turn_id)
            record = self._to_record(row)
            emit_run_state_event(
                task_id=task_id,
                state=record.state,
                turn_id=record.turn_id,
                cancel_requested=record.cancel_requested,
                cancel_reason=record.cancel_reason,
                conversation_id=record.conversation_id,
            )
            return {
                "cancelled": True,
                "already_cancelled": False,
                "active": True,
                "turn_id": row.turn_id,
                "cancel_reason": cancel_reason,
            }
        except Exception:
            logger.warning(
                "Falling back to in-memory lifecycle cancel (task=%s turn_id=%s)",
                task_id,
                turn_id,
                exc_info=True,
            )
            result = self._registry.request_cancel(
                task_id=task_id,
                turn_id=turn_id,
                reason=reason,
            )
            if result.get("cancelled"):
                self._cancel_registered_task(
                    task_id=task_id,
                    turn_id=str(result.get("turn_id") or turn_id or "") or None,
                )
            active = self._registry.get_active(task_id)
            if active is not None:
                emit_run_state_event(
                    task_id=task_id,
                    state=active.state,
                    turn_id=active.turn_id,
                    cancel_requested=active.cancel_requested,
                    cancel_reason=active.cancel_reason,
                    conversation_id=active.conversation_id,
                )
            return result
        finally:
            if owns_session:
                session.close()

    def is_cancel_requested(
        self,
        *,
        task_id: int,
        turn_id: Optional[str] = None,
        db_session: Optional[Session] = None,
    ) -> bool:
        if self._registry.is_cancel_requested(task_id=task_id, turn_id=turn_id):
            return True
        session, owns_session = self._open_session(db_session)
        try:
            if turn_id:
                row = (
                    session.query(TurnWorkflow)
                    .filter(TurnWorkflow.task_id == task_id, TurnWorkflow.turn_id == turn_id)
                    .order_by(desc(TurnWorkflow.id))
                    .first()
                )
                if row is not None:
                    metadata = self._metadata(row)
                    terminal_cancelled = str(metadata.get("terminal_status") or "").strip().lower() == "cancelled"
                    requested = bool(metadata.get("cancel_requested")) or terminal_cancelled
                    if requested and not terminal_cancelled:
                        self._sync_registry_cancel(
                            task_id=row.task_id,
                            turn_id=row.turn_id,
                            reason=(
                                metadata.get("cancel_reason")
                                if isinstance(metadata.get("cancel_reason"), str)
                                else None
                            ),
                        )
                    return requested
            query = session.query(TurnWorkflow).filter(
                TurnWorkflow.task_id == task_id,
                TurnWorkflow.state.in_(list(_ACTIVE_STATES)),
            )
            row = query.order_by(desc(TurnWorkflow.updated_at), desc(TurnWorkflow.id)).first()
            if row is None:
                return False
            requested = bool(self._metadata(row).get("cancel_requested"))
            if requested:
                self._sync_registry_cancel(
                    task_id=row.task_id,
                    turn_id=row.turn_id,
                    reason=(self._metadata(row).get("cancel_reason") if isinstance(self._metadata(row).get("cancel_reason"), str) else None),
                )
            return requested
        except Exception:
            return self._registry.is_cancel_requested(task_id=task_id, turn_id=turn_id)
        finally:
            if owns_session:
                session.close()

    def end_run(
        self,
        *,
        task_id: int,
        turn_id: str,
        status: str,
        db_session: Optional[Session] = None,
    ) -> None:
        normalized = (status or "failed").strip().lower()
        session, owns_session = self._open_session(db_session)
        try:
            row = (
                session.query(TurnWorkflow)
                .filter(TurnWorkflow.task_id == task_id, TurnWorkflow.turn_id == turn_id)
                .order_by(desc(TurnWorkflow.id))
                .first()
            )
            if row is None:
                self._registry.finish(task_id=task_id, turn_id=turn_id, state=normalized)
                return
            self._project_end_status(row, status=normalized)
            session.commit()
            session.refresh(row)
            record = self._to_record(row)
            self._registry.finish(task_id=task_id, turn_id=turn_id, state=record.state)
            emit_run_state_event(
                task_id=task_id,
                state=record.state,
                turn_id=record.turn_id,
                cancel_requested=record.cancel_requested,
                cancel_reason=record.cancel_reason,
                conversation_id=record.conversation_id,
            )
        except Exception:
            logger.warning(
                "Falling back to in-memory lifecycle end (task=%s turn_id=%s status=%s)",
                task_id,
                turn_id,
                status,
                exc_info=True,
            )
            self._registry.finish(task_id=task_id, turn_id=turn_id, state=normalized)
            emit_run_state_event(
                task_id=task_id,
                state=normalized,
                turn_id=turn_id,
                cancel_requested=(normalized == "cancelled"),
                cancel_reason=("explicit_cancel" if normalized == "cancelled" else None),
                conversation_id=None,
            )
        finally:
            if owns_session:
                session.close()


_shared_run_lifecycle = RunLifecycleService()


def get_run_lifecycle_service() -> RunLifecycleService:
    """Return the process-global run lifecycle service."""
    return _shared_run_lifecycle


__all__ = ["RunLifecycleService", "get_run_lifecycle_service"]
