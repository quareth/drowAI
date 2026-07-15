"""Waiting transition helpers for interrupted turn execution outcomes.

This module centralizes WAITING_FOR_HUMAN payload shaping and workflow
transition dispatch for start/resume/retry flows.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple


class TurnExecutionWaitingTransitionService:
    """Service that applies waiting transitions for interrupted runs."""

    def handle_start_interruption(
        self,
        *,
        task_id: int,
        workflow_id: Optional[int],
        turn_id: Optional[str],
        reserved_message_id: Optional[int],
        result_metadata: Dict[str, Any],
        context_window_metadata: Optional[Dict[str, Any]],
        compression_metadata: Optional[Dict[str, Any]],
        mark_turn_workflow_waiting: Optional[Callable[..., None]],
        mark_turn_workflow_waiting_best_effort: Callable[..., None],
        context_window_handoff_fields: Callable[[Optional[Dict[str, Any]]], Dict[str, Any]],
        compression_handoff_fields: Callable[[Optional[Dict[str, Any]]], Dict[str, Any]],
    ) -> Tuple[bool, Optional[int]]:
        """Apply waiting transition for start flow when interrupted."""
        if not result_metadata.get("interrupted"):
            return False, reserved_message_id

        interrupt_type = result_metadata.get("interrupt_type")
        interrupt_graph_name = result_metadata.get("graph_name")
        interrupt_checkpoint_id: Optional[str] = None
        workflow_reserved_message_id = reserved_message_id
        metadata_checkpoint = result_metadata.get("checkpoint_id")
        if isinstance(metadata_checkpoint, (int, str)):
            interrupt_checkpoint_id = str(metadata_checkpoint)
        metadata_reserved_message_id = result_metadata.get("reserved_message_id")
        if isinstance(metadata_reserved_message_id, int):
            workflow_reserved_message_id = metadata_reserved_message_id
            if reserved_message_id is None:
                reserved_message_id = metadata_reserved_message_id
        interrupt_resume_key = (
            interrupt_checkpoint_id
            or f"{interrupt_graph_name or 'unknown'}:{task_id}:{turn_id or 'unknown'}"
        )
        workflow_waiting_fn = mark_turn_workflow_waiting or mark_turn_workflow_waiting_best_effort
        waiting_metadata = {"interrupted": True}
        waiting_metadata.update(context_window_handoff_fields(context_window_metadata))
        waiting_metadata.update(compression_handoff_fields(compression_metadata))
        workflow_waiting_fn(
            workflow_id=workflow_id,
            checkpoint_id=interrupt_checkpoint_id,
            interrupt_type=interrupt_type,
            graph_name=interrupt_graph_name if isinstance(interrupt_graph_name, str) else None,
            reserved_message_id=workflow_reserved_message_id,
            resume_key=interrupt_resume_key,
            metadata=waiting_metadata,
        )
        return True, reserved_message_id

    def handle_resume_interruption(
        self,
        *,
        task_id: int,
        workflow_id: Optional[int],
        graph_name: Optional[str],
        checkpoint_id: Optional[int | str],
        resume_key: Optional[str],
        reserved_message_id: Optional[int],
        result_metadata: Dict[str, Any],
        context_window_metadata: Optional[Dict[str, Any]],
        mark_turn_workflow_waiting: Optional[Callable[..., None]],
        mark_turn_workflow_waiting_best_effort: Callable[..., None],
        context_window_handoff_fields: Callable[[Optional[Dict[str, Any]]], Dict[str, Any]],
        compression_handoff_fields: Callable[[Optional[Dict[str, Any]]], Dict[str, Any]],
    ) -> Tuple[bool, Optional[int]]:
        """Apply waiting transition for resume flow when interrupted."""
        if not result_metadata.get("interrupt"):
            return False, reserved_message_id

        interrupt_type = result_metadata.get("interrupt_type")
        interrupt_graph_name = result_metadata.get("graph_name") or graph_name
        interrupt_checkpoint_id = str(checkpoint_id) if checkpoint_id is not None else None
        metadata_checkpoint = result_metadata.get("checkpoint_id")
        if isinstance(metadata_checkpoint, (int, str)):
            interrupt_checkpoint_id = str(metadata_checkpoint)
        metadata_reserved_message_id = result_metadata.get("reserved_message_id")
        if isinstance(metadata_reserved_message_id, int):
            reserved_message_id = metadata_reserved_message_id
        interrupt_resume_key = (
            interrupt_checkpoint_id
            or resume_key
            or f"{interrupt_graph_name or 'unknown'}:{task_id}:unknown"
        )
        workflow_waiting_fn = mark_turn_workflow_waiting or mark_turn_workflow_waiting_best_effort
        waiting_metadata = {"resume_interrupted": True}
        waiting_metadata.update(context_window_handoff_fields(context_window_metadata))
        waiting_metadata.update(compression_handoff_fields(result_metadata))
        workflow_waiting_fn(
            workflow_id=workflow_id,
            checkpoint_id=interrupt_checkpoint_id,
            interrupt_type=interrupt_type,
            graph_name=interrupt_graph_name if isinstance(interrupt_graph_name, str) else graph_name,
            reserved_message_id=reserved_message_id,
            resume_key=interrupt_resume_key,
            metadata=waiting_metadata,
        )
        return True, reserved_message_id

    def handle_retry_interruption(
        self,
        *,
        task_id: int,
        workflow_id: int,
        graph_name: str,
        lifecycle_turn_id: Optional[str],
        reserved_message_id: Optional[int],
        result_metadata: Dict[str, Any],
        context_window_metadata: Optional[Dict[str, Any]],
        mark_turn_workflow_waiting: Optional[Callable[..., None]],
        mark_turn_workflow_waiting_best_effort: Callable[..., None],
        context_window_handoff_fields: Callable[[Optional[Dict[str, Any]]], Dict[str, Any]],
        compression_handoff_fields: Callable[[Optional[Dict[str, Any]]], Dict[str, Any]],
    ) -> Tuple[bool, Optional[int]]:
        """Apply waiting transition for checkpoint-retry flow when interrupted."""
        if not result_metadata.get("interrupt"):
            return False, reserved_message_id

        interrupt_type = result_metadata.get("interrupt_type")
        interrupt_graph_name = result_metadata.get("graph_name") if isinstance(result_metadata, dict) else graph_name
        interrupt_checkpoint_id: Optional[str] = None
        metadata_checkpoint = result_metadata.get("checkpoint_id")
        if isinstance(metadata_checkpoint, (int, str)):
            interrupt_checkpoint_id = str(metadata_checkpoint)
        metadata_reserved_message_id = result_metadata.get("reserved_message_id")
        if isinstance(metadata_reserved_message_id, int):
            reserved_message_id = metadata_reserved_message_id
        retry_resume_key = (
            interrupt_checkpoint_id
            or f"{interrupt_graph_name or graph_name}:{task_id}:{lifecycle_turn_id or 'unknown'}"
        )
        workflow_waiting_fn = mark_turn_workflow_waiting or mark_turn_workflow_waiting_best_effort
        # Phase 4.3: clear the in-flight ``active_retry`` block and stamp
        # ``retry_state=waiting_for_human`` so transcript bootstrap can
        # derive the waiting overlay from one workflow row read.
        waiting_metadata: Dict[str, Any] = {
            "retry_interrupted": True,
            "active_retry": None,
            "retry_state": "waiting_for_human",
        }
        waiting_metadata.update(context_window_handoff_fields(context_window_metadata))
        waiting_metadata.update(compression_handoff_fields(result_metadata))
        workflow_waiting_fn(
            workflow_id=workflow_id,
            checkpoint_id=interrupt_checkpoint_id,
            interrupt_type=interrupt_type,
            graph_name=interrupt_graph_name if isinstance(interrupt_graph_name, str) else graph_name,
            reserved_message_id=reserved_message_id,
            resume_key=retry_resume_key,
            metadata=waiting_metadata,
        )
        return True, reserved_message_id
