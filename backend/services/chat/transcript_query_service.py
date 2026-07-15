"""
Chat transcript read-model service for startup and history pagination.

Responsibilities:
- Resolve effective conversation identity for transcript reads.
- Query compact transcript pages from ChatMessage persistence with detail ordering from ChatTurnEvent.
- Provide deterministic cursor-based pagination by canonical turn identity.

Hot-path prompt boundary
------------------------
This service is strictly a UI / pagination read model. It must NOT be
used as the prompt-authoritative conversation-history source.
Prompt-facing recent-transcript text is produced exclusively by:

    ConversationHistoryReader.build_openai_conversation_history
      -> ConversationContextBundle.transcript_window
      -> agent.graph.context.serialization.render_recent_transcript
      -> agent.graph.context.serialization.serialize_projection_to_prompt_sections

Reusing this service in the hot path would introduce a second
conversation-history authority, couple LLM prompt assembly to a UI
read model, and break cache-prefix stability. If a prompt role needs
transcript text, it must go through the shared serializer above.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.models.chat import ChatMessage, ChatTurnEvent
from backend.models.hitl import TurnWorkflow
from backend.models.provenance import ToolExecution
from backend.services.langgraph_chat.checkpoint.turn_workflow_service import (
    DEFAULT_CHECKPOINT_RETRY_MAX_ATTEMPTS,
    TurnWorkflowState,
)

TranscriptKind = Literal["user", "assistant", "reasoning", "tool", "observation"]

_USER_MESSAGE_TYPES = {"user", "user_message", "user_input"}
_ASSISTANT_MESSAGE_TYPES = {"assistant", "assistant_message"}
_EXCLUDED_MESSAGE_TYPES = {"system", "system_summary"}
_DETAIL_KINDS = {"tool", "observation", "reasoning"}
_CANCELLED_TOOL_EXECUTION_STATUSES: frozenset[str] = frozenset(
    {"cancel_requested", "cancelled", "canceled", "stopped"}
)

# Workflow states that drive retry-aware projection metadata. Any state not in
# this set is treated as "no retry overlay" — the assistant message keeps its
# default rendering and inherits no retry CTA / retrying badge.
_RETRY_PROJECTION_STATES: frozenset[str] = frozenset(
    {
        TurnWorkflowState.RETRYING.value,
        TurnWorkflowState.WAITING_FOR_HUMAN.value,
        TurnWorkflowState.FAILED.value,
        TurnWorkflowState.COMPLETED.value,
    }
)


def _has_chat_stop_cancellation_projection(execution: ToolExecution) -> bool:
    """Return whether a tool execution should hydrate as stopped in chat history."""
    status = str(getattr(execution, "status", "") or "").strip().lower()
    if status in _CANCELLED_TOOL_EXECUTION_STATUSES:
        return True
    metadata = getattr(execution, "execution_metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    cancellation = metadata.get("cancellation")
    if not isinstance(cancellation, dict):
        return False
    if bool(cancellation.get("cancel_requested")):
        return True
    return str(cancellation.get("source") or "").strip() == "chat_stop"


@dataclass(frozen=True)
class TranscriptCursor:
    """Deterministic transcript cursor keyed by canonical turn number."""

    turn_number: int


@dataclass(frozen=True)
class ChatTranscriptItem:
    """Render-ready compact transcript item."""

    id: str
    kind: TranscriptKind
    turn_number: int
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatTranscriptPage:
    """One transcript page and its older-pagination cursor."""

    conversation_id: str
    items: List[ChatTranscriptItem]
    has_more_older: bool
    next_before: Optional[TranscriptCursor]


class ChatTranscriptQueryService:
    """Read-model query service for compact transcript pages."""

    def __init__(self, db: Session):
        self.db = db

    def resolve_existing_conversation_id(
        self,
        task_id: int,
        requested_conversation_id: Optional[str],
    ) -> Optional[str]:
        """Resolve an existing conversation id for read-only operations."""
        requested = (requested_conversation_id or "").strip()
        if requested:
            return requested
        return self._select_latest_conversation_id(task_id)

    def resolve_conversation_id(self, task_id: int, requested_conversation_id: Optional[str]) -> str:
        """Resolve the conversation id used for transcript reads."""
        return self.resolve_existing_conversation_id(task_id, requested_conversation_id) or ""

    def _select_latest_conversation_id(self, task_id: int) -> Optional[str]:
        """Select the newest persisted conversation id for a task."""
        turn_key = self._turn_key_expression()
        row = self.db.execute(
            select(ChatMessage.conversation_id)
            .where(ChatMessage.task_id == task_id)
            .where(ChatMessage.conversation_id.isnot(None))
            .where(func.length(func.trim(ChatMessage.conversation_id)) > 0)
            .order_by(turn_key.desc(), ChatMessage.id.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        resolved = str(row[0] or "").strip()
        return resolved or None

    def list_latest_transcript_page(
        self,
        *,
        task_id: int,
        requested_conversation_id: Optional[str] = None,
        limit: int,
    ) -> ChatTranscriptPage:
        """Return the latest transcript page for a conversation."""
        if limit <= 0:
            raise ValueError("limit must be > 0")
        conversation_id = self.resolve_conversation_id(task_id, requested_conversation_id)
        if not conversation_id:
            return ChatTranscriptPage(
                conversation_id="",
                items=[],
                has_more_older=False,
                next_before=None,
            )
        selected_turns, has_more_older = self._select_page_turns(
            task_id=task_id,
            conversation_id=conversation_id,
            before=None,
            limit=limit,
        )
        messages = self._select_messages_for_turns(
            task_id=task_id,
            conversation_id=conversation_id,
            turn_numbers=selected_turns,
        )
        items = self._map_messages_to_items(messages)
        next_before = self._cursor_from_oldest_turn(selected_turns) if has_more_older else None
        return ChatTranscriptPage(
            conversation_id=conversation_id,
            items=items,
            has_more_older=has_more_older,
            next_before=next_before,
        )

    def list_older_transcript_page(
        self,
        *,
        task_id: int,
        before: TranscriptCursor,
        requested_conversation_id: Optional[str] = None,
        limit: int,
    ) -> ChatTranscriptPage:
        """Return one page older than the provided cursor."""
        if limit <= 0:
            raise ValueError("limit must be > 0")
        conversation_id = self.resolve_conversation_id(task_id, requested_conversation_id)
        if not conversation_id:
            return ChatTranscriptPage(
                conversation_id="",
                items=[],
                has_more_older=False,
                next_before=None,
            )
        selected_turns, has_more_older = self._select_page_turns(
            task_id=task_id,
            conversation_id=conversation_id,
            before=before,
            limit=limit,
        )
        messages = self._select_messages_for_turns(
            task_id=task_id,
            conversation_id=conversation_id,
            turn_numbers=selected_turns,
        )
        items = self._map_messages_to_items(messages)
        next_before = self._cursor_from_oldest_turn(selected_turns) if has_more_older else None
        return ChatTranscriptPage(
            conversation_id=conversation_id,
            items=items,
            has_more_older=has_more_older,
            next_before=next_before,
        )

    def resolve_before_cursor(
        self,
        *,
        task_id: int,
        conversation_id: str,
        before_turn_number: int,
    ) -> Optional[TranscriptCursor]:
        """Resolve a transcript cursor from a turn number in one conversation."""
        if before_turn_number <= 0:
            return None
        turn_key = self._turn_key_expression()
        row = self.db.execute(
            select(ChatMessage.id).where(
                ChatMessage.task_id == task_id,
                ChatMessage.conversation_id == conversation_id,
                turn_key == before_turn_number,
            )
        ).first()
        if row is None:
            return None
        return TranscriptCursor(turn_number=before_turn_number)

    @staticmethod
    def _turn_key_expression():
        """Canonical SQL expression used for transcript turn ordering."""
        return func.coalesce(ChatMessage.turn_number, ChatMessage.id)

    def _base_message_query(self, *, task_id: int, conversation_id: str):
        """Shared base message query for transcript reads."""
        return (
            select(ChatMessage)
            .where(
                ChatMessage.task_id == task_id,
                ChatMessage.conversation_id == conversation_id,
            )
            .where(~func.lower(ChatMessage.message_type).in_(_EXCLUDED_MESSAGE_TYPES))
        )

    def _select_page_turns(
        self,
        *,
        task_id: int,
        conversation_id: str,
        before: Optional[TranscriptCursor],
        limit: int,
    ) -> tuple[List[int], bool]:
        """Select one page of canonical turn numbers for transcript pagination."""
        turn_key = self._turn_key_expression().label("turn_key")
        query = (
            select(turn_key)
            .where(
                ChatMessage.task_id == task_id,
                ChatMessage.conversation_id == conversation_id,
            )
            .where(~func.lower(ChatMessage.message_type).in_(_EXCLUDED_MESSAGE_TYPES))
        )
        if before is not None:
            query = query.where(turn_key < before.turn_number)
        query = query.distinct().order_by(turn_key.desc()).limit(limit + 1)
        selected_turns: List[int] = []
        for turn in self.db.execute(query).scalars().all():
            if turn is None:
                continue
            try:
                selected_turns.append(int(turn))
            except (TypeError, ValueError):
                continue
        has_more = len(selected_turns) > limit
        if has_more:
            selected_turns = selected_turns[:limit]
        return selected_turns, has_more

    def _select_messages_for_turns(
        self,
        *,
        task_id: int,
        conversation_id: str,
        turn_numbers: List[int],
    ) -> List[ChatMessage]:
        """Load all ChatMessage rows that belong to selected canonical turns."""
        if not turn_numbers:
            return []
        turn_key = self._turn_key_expression()
        query = (
            self._base_message_query(task_id=task_id, conversation_id=conversation_id)
            .where(turn_key.in_(turn_numbers))
            .order_by(turn_key.asc(), ChatMessage.id.asc())
        )
        return list(self.db.execute(query).scalars().unique().all())

    def _cursor_from_oldest_turn(self, turn_numbers: List[int]) -> Optional[TranscriptCursor]:
        """Build the next older-page cursor from the oldest selected turn."""
        if not turn_numbers:
            return None
        return TranscriptCursor(turn_number=min(turn_numbers))

    def _map_messages_to_items(self, messages: List[ChatMessage]) -> List[ChatTranscriptItem]:
        """Map ChatMessage rows and canonical ChatTurnEvent details into transcript items."""
        items: List[ChatTranscriptItem] = []
        canonical_events_by_message = self._load_canonical_turn_events(messages)
        workflow_projection_by_message = self._load_workflow_projection_metadata(messages)
        cancelled_tool_executions_by_message = self._load_cancelled_tool_execution_projection(
            messages,
            workflow_projection_by_message,
        )
        # When a workflow is mid-retry (RETRYING), canonical detail rows from
        # the previous attempt would appear interleaved with the live
        # re-rendering until the resync primitive replays the new attempt's
        # events. Suppress those stale detail rows in the bootstrap transcript
        # so the frontend never sees an inconsistent view. Stream events
        # themselves are not deleted; only the projection hides rows the active
        # attempt has not yet replaced.
        active_retry_message_ids: set[int] = {
            message_id
            for message_id, projection in workflow_projection_by_message.items()
            if bool(projection.get("active_retry"))
        }
        for message in messages:
            turn_number = int(getattr(message, "turn_number", 0) or message.id)
            message_type = str(getattr(message, "message_type", "") or "").strip().lower()
            content = str(getattr(message, "message", "") or "")
            timestamp = self._serialize_timestamp(getattr(message, "created_at", None))
            sequence_seed = int(getattr(message, "id", 0) or 0) * 1000
            sequence_counter = 0

            def next_sequence() -> int:
                nonlocal sequence_counter
                sequence_counter += 1
                return sequence_seed + sequence_counter

            message_sequence = next_sequence()
            message_metadata: Dict[str, Any] = {
                "message_id": message.id,
                "conversation_id": message.conversation_id,
                "message_type": message_type,
                "sequence": message_sequence,
                "sequence_authority": "synthetic_message",
            }
            if timestamp:
                message_metadata["timestamp"] = timestamp
            # Surface the persisted error code on every transcript item so the
            # frontend can route originally-errored messages through the
            # plain-text renderer regardless of the workflow's *current*
            # status overlay (RETRYING/WAITING_FOR_HUMAN/COMPLETED do not set
            # ``error_code`` themselves, which would otherwise let the
            # streaming JSON detector trip on the persisted ``[Error] …``
            # content during a retry pause). The FAILED projection's
            # ``error_code`` overlay still wins via ``message_metadata.update``.
            persisted_error = getattr(message, "error", None)
            if isinstance(persisted_error, str) and persisted_error.strip():
                message_metadata["error_code"] = persisted_error.strip()
            workflow_metadata = workflow_projection_by_message.get(int(getattr(message, "id", 0) or 0))
            if workflow_metadata:
                # Strip projection-internal sentinel before merging onto the
                # rendered transcript item.
                projected = {
                    key: value
                    for key, value in workflow_metadata.items()
                    if not key.startswith("__")
                }
                message_metadata.update(projected)
            items.append(
                ChatTranscriptItem(
                    id=f"msg-{message.id}",
                    kind=self._message_kind(message_type),
                    turn_number=turn_number,
                    content=content,
                    metadata=message_metadata,
                )
            )
            message_id_int = int(getattr(message, "id", 0) or 0)
            suppress_stale_details = message_id_int in active_retry_message_ids
            canonical_events = canonical_events_by_message.get(message_id_int, [])
            has_canonical_reasoning = any(
                str(getattr(evt, "kind", "") or "").strip().lower() == "reasoning"
                for evt in canonical_events
            )

            # Prefer canonical reasoning rows from chat_turn_events when
            # available; fall back to the legacy reasoning_tokens blob only
            # for older messages that predate canonical reasoning persistence.
            if not has_canonical_reasoning and not suppress_stale_details:
                reasoning_tokens = getattr(message, "reasoning_tokens", None)
                if isinstance(reasoning_tokens, str) and reasoning_tokens.strip():
                    reasoning_sequence = next_sequence()
                    reasoning_metadata: Dict[str, Any] = {
                        "message_id": message.id,
                        "ind": 0,
                        "sequence": reasoning_sequence,
                        "sequence_authority": "legacy_reasoning_blob",
                        "phase_sequence": 0,
                        "reasoning_section_id": f"msg-{message.id}-reasoning-0",
                    }
                    if timestamp:
                        reasoning_metadata["timestamp"] = timestamp
                    items.append(
                        ChatTranscriptItem(
                            id=f"msg-{message.id}-reasoning",
                            kind="reasoning",
                            turn_number=turn_number,
                            content=reasoning_tokens,
                            metadata=reasoning_metadata,
                        )
                    )
            if not suppress_stale_details:
                items.extend(
                    self._map_turn_detail_items(
                        message=message,
                        turn_number=turn_number,
                        timestamp=timestamp,
                        canonical_events=canonical_events,
                    )
                )
                existing_tool_call_ids = {
                    str(getattr(evt, "tool_call_id", "") or "")
                    for evt in canonical_events
                    if str(getattr(evt, "kind", "") or "").strip().lower() == "tool"
                    and getattr(evt, "tool_call_id", None)
                }
                items.extend(
                    self._map_cancelled_tool_execution_items(
                        message=message,
                        turn_number=turn_number,
                        timestamp=timestamp,
                        executions=cancelled_tool_executions_by_message.get(message_id_int, []),
                        existing_tool_call_ids=existing_tool_call_ids,
                    )
                )
        return items

    def _load_cancelled_tool_execution_projection(
        self,
        messages: List[ChatMessage],
        workflow_projection_by_message: Dict[int, Dict[str, Any]],
    ) -> Dict[int, List[ToolExecution]]:
        """Load stopped tool executions for cancelled turns in the UI read model."""
        message_keys: Dict[int, tuple[int, str, str]] = {}
        for message in messages:
            message_id = int(getattr(message, "id", 0) or 0)
            projection = workflow_projection_by_message.get(message_id)
            if not projection or projection.get("status") != "cancelled":
                continue
            task_id = int(getattr(message, "task_id", 0) or 0)
            conversation_id = str(getattr(message, "conversation_id", "") or "").strip()
            turn_id = str(projection.get("turn_id") or projection.get("id") or "").strip()
            if task_id <= 0 or not conversation_id or not turn_id:
                continue
            message_keys[message_id] = (task_id, conversation_id, turn_id)

        if not message_keys:
            return {}

        task_ids = {task_id for task_id, _, _ in message_keys.values()}
        conversation_ids = {conversation_id for _, conversation_id, _ in message_keys.values()}
        turn_ids = {turn_id for _, _, turn_id in message_keys.values()}
        query = (
            select(ToolExecution)
            .where(
                ToolExecution.task_id.in_(task_ids),
                ToolExecution.conversation_id.in_(conversation_ids),
                ToolExecution.turn_id.in_(turn_ids),
            )
            .order_by(ToolExecution.created_at.asc(), ToolExecution.id.asc())
        )
        rows = list(self.db.execute(query).scalars().all())
        rows_by_key: Dict[tuple[int, str, str], List[ToolExecution]] = {}
        for row in rows:
            if not _has_chat_stop_cancellation_projection(row):
                continue
            key = (
                int(getattr(row, "task_id", 0) or 0),
                str(getattr(row, "conversation_id", "") or "").strip(),
                str(getattr(row, "turn_id", "") or "").strip(),
            )
            rows_by_key.setdefault(key, []).append(row)

        projected: Dict[int, List[ToolExecution]] = {}
        for message_id, key in message_keys.items():
            rows_for_message = rows_by_key.get(key)
            if rows_for_message:
                projected[message_id] = rows_for_message
        return projected

    def _load_workflow_projection_metadata(
        self,
        messages: List[ChatMessage],
    ) -> Dict[int, Dict[str, Any]]:
        """Project workflow rows onto retry-aware transcript metadata.

        Keyed by the reserved assistant ``message_id``. The projection respects
        the persisted workflow ``state`` so a successful retry never inherits
        stale ``retryable`` / ``status="error"`` overlays from the previous
        attempt and an in-flight retry surfaces as ``status="retrying"`` with
        ``retryable=False``.

        Output may include reserved ``__active_attempt`` keys consumed by the
        detail-row projection. Reserved keys are stripped before the metadata
        reaches transcript items.
        """
        assistant_message_ids = [
            int(getattr(message, "id", 0) or 0)
            for message in messages
            if int(getattr(message, "id", 0) or 0) > 0
            and str(getattr(message, "message_type", "") or "").strip().lower() in _ASSISTANT_MESSAGE_TYPES
        ]
        if not assistant_message_ids:
            return {}

        query = (
            select(TurnWorkflow)
            .where(TurnWorkflow.reserved_message_id.in_(assistant_message_ids))
            .order_by(
                TurnWorkflow.reserved_message_id.asc(),
                TurnWorkflow.updated_at.desc(),
                TurnWorkflow.id.desc(),
            )
        )
        rows = list(self.db.execute(query).scalars().all())
        metadata_by_message: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            message_id = int(getattr(row, "reserved_message_id", 0) or 0)
            if message_id <= 0 or message_id in metadata_by_message:
                continue
            state = str(getattr(row, "state", "") or "").strip().upper()
            if state not in _RETRY_PROJECTION_STATES:
                continue
            projected = self._project_workflow_row(row=row, state=state)
            if projected is not None:
                metadata_by_message[message_id] = projected
        return metadata_by_message

    @staticmethod
    def _project_workflow_row(
        *,
        row: TurnWorkflow,
        state: str,
    ) -> Optional[Dict[str, Any]]:
        """Build retry-aware overlay metadata for one workflow row.

        Returns ``None`` when the row carries nothing actionable (e.g. a plain
        COMPLETED row with no retry history). Reserved ``__*`` keys are
        consumed by the detail-row projection and never reach the wire.
        """
        workflow_metadata = getattr(row, "workflow_metadata", None)
        workflow_metadata = workflow_metadata if isinstance(workflow_metadata, dict) else {}
        graph_name = getattr(row, "graph_name", None)
        turn_id_raw = getattr(row, "turn_id", None)
        turn_id = turn_id_raw.strip() if isinstance(turn_id_raw, str) else None
        turn_sequence = getattr(row, "turn_sequence", None)
        retry_mode = workflow_metadata.get("retry_mode") or "checkpoint"

        retry_attempt_count = workflow_metadata.get("retry_attempt_count")
        retry_attempt = (
            int(retry_attempt_count)
            if isinstance(retry_attempt_count, int)
            else None
        )
        raw_max = workflow_metadata.get("retry_max_attempts")
        retry_max_attempts = (
            int(raw_max)
            if isinstance(raw_max, int) and raw_max > 0
            else DEFAULT_CHECKPOINT_RETRY_MAX_ATTEMPTS
        )

        common: Dict[str, Any] = {
            "graph_name": graph_name,
            "turn_id": turn_id,
        }
        if turn_id:
            common["id"] = turn_id
        if isinstance(turn_sequence, int):
            common["turn_sequence"] = turn_sequence
        if retry_attempt is not None:
            common["retry_attempt"] = retry_attempt
        common["retry_max_attempts"] = retry_max_attempts
        common["retry_mode"] = retry_mode

        if state == TurnWorkflowState.RETRYING.value:
            # Active retry — render as in-flight, hide CTA, signal resync.
            overlay: Dict[str, Any] = dict(common)
            overlay.update(
                {
                    "status": "retrying",
                    "retry_state": "retrying",
                    "retryable": False,
                    "another_retry_allowed": False,
                    "active_retry": True,
                }
            )
            checkpoint_id = getattr(row, "checkpoint_id", None)
            if isinstance(checkpoint_id, str) and checkpoint_id.strip():
                overlay["checkpoint_id"] = checkpoint_id.strip()
            if retry_attempt is not None:
                overlay["__active_attempt"] = retry_attempt
            return {key: value for key, value in overlay.items() if value is not None}

        if state == TurnWorkflowState.WAITING_FOR_HUMAN.value:
            # Waiting for human input. Retry CTA must stay disabled while
            # the workflow is awaiting interaction.
            overlay = dict(common)
            overlay.update(
                {
                    "status": "waiting_for_human",
                    "retry_state": "waiting_for_human",
                    "retryable": False,
                    "another_retry_allowed": False,
                }
            )
            interrupt_type = getattr(row, "interrupt_type", None)
            if isinstance(interrupt_type, str) and interrupt_type.strip():
                overlay["interrupt_type"] = interrupt_type.strip()
            return {key: value for key, value in overlay.items() if value is not None}

        if state == TurnWorkflowState.FAILED.value:
            if workflow_metadata.get("outcome_type") == "provider_refusal":
                refusal = workflow_metadata.get("refusal")
                refusal = dict(refusal) if isinstance(refusal, dict) else {}
                overlay = dict(common)
                overlay.update(
                    {
                        "status": "declined",
                        "stop_reason": "refusal",
                        "outcome_type": "provider_refusal",
                        "retryable": False,
                        "another_retry_allowed": False,
                        "active_retry": False,
                        "refusal": refusal,
                    }
                )
                return {
                    key: value for key, value in overlay.items() if value is not None
                }

            retry_state = (
                workflow_metadata.get("retry_state").strip().lower()
                if isinstance(workflow_metadata.get("retry_state"), str)
                else None
            )
            terminal_status = (
                workflow_metadata.get("terminal_status").strip().lower()
                if isinstance(workflow_metadata.get("terminal_status"), str)
                else None
            )
            if (
                retry_state == "cancelled"
                or terminal_status == "cancelled"
            ):
                overlay = dict(common)
                overlay.update(
                    {
                        "status": "cancelled",
                        "retry_state": "cancelled",
                        "retryable": False,
                        "another_retry_allowed": False,
                        "retry_exhausted": False,
                        "active_retry": False,
                        "error_code": (
                            workflow_metadata.get("error_code")
                            or workflow_metadata.get("error")
                        ),
                        "error_message": workflow_metadata.get("error_message"),
                    }
                )
                checkpoint_id = getattr(row, "checkpoint_id", None)
                if isinstance(checkpoint_id, str) and checkpoint_id.strip():
                    overlay["checkpoint_id"] = checkpoint_id.strip()
                return {
                    key: value for key, value in overlay.items() if value is not None
                }

            # FAILED workflow — show CTA only when row is marked retryable
            # AND the backend retry budget has room AND a stable
            # ``checkpoint_id`` is persisted. Checkpoint retry must fail
            # safe: legacy/invalid retryable rows without a checkpoint id
            # cannot be retried because the worker has nothing to resume
            # from, so the CTA is suppressed even when ``retryable`` is set.
            retryable_flag = bool(workflow_metadata.get("retryable"))
            attempt_for_gating = retry_attempt if retry_attempt is not None else 0
            raw_checkpoint = getattr(row, "checkpoint_id", None)
            normalized_checkpoint = (
                raw_checkpoint.strip()
                if isinstance(raw_checkpoint, str) and raw_checkpoint.strip()
                else None
            )
            has_checkpoint = bool(normalized_checkpoint)
            another_retry_allowed = (
                retryable_flag
                and attempt_for_gating < retry_max_attempts
                and has_checkpoint
            )
            retry_exhausted = (
                retryable_flag and attempt_for_gating >= retry_max_attempts
            )
            overlay = dict(common)
            overlay.update(
                {
                    "status": "error",
                    "retry_state": "failed",
                    "retryable": another_retry_allowed,
                    "another_retry_allowed": another_retry_allowed,
                    "retry_exhausted": retry_exhausted,
                    "error_code": (
                        workflow_metadata.get("error_code")
                        or workflow_metadata.get("error")
                    ),
                    "error_message": workflow_metadata.get("error_message"),
                    "failure_stage": workflow_metadata.get("failure_stage"),
                }
            )
            if normalized_checkpoint:
                overlay["checkpoint_id"] = normalized_checkpoint
            return {key: value for key, value in overlay.items() if value is not None}

        if state == TurnWorkflowState.COMPLETED.value:
            # COMPLETED workflow — this resolves a previously failed/retrying
            # attempt. Do NOT inherit the retryable error overlay; render the
            # active assistant message with a clean completed badge so the
            # frontend never reopens the retry CTA after a successful retry.
            overlay = dict(common)
            overlay.update(
                {
                    "status": "completed",
                    "retry_state": "completed",
                    "retryable": False,
                    "another_retry_allowed": False,
                    "active_retry": False,
                }
            )
            if retry_attempt is not None:
                overlay["__active_attempt"] = retry_attempt
            return {key: value for key, value in overlay.items() if value is not None}

        return None

    def _load_canonical_turn_events(
        self,
        messages: List[ChatMessage],
    ) -> Dict[int, List[ChatTurnEvent]]:
        """Load canonical turn events by message id for selected assistant rows."""
        assistant_message_ids = [
            int(getattr(message, "id", 0) or 0)
            for message in messages
            if int(getattr(message, "id", 0) or 0) > 0
            and str(getattr(message, "message_type", "") or "").strip().lower() in _ASSISTANT_MESSAGE_TYPES
        ]
        if not assistant_message_ids:
            return {}

        query = (
            select(ChatTurnEvent)
            .where(ChatTurnEvent.chat_message_id.in_(assistant_message_ids))
            .order_by(ChatTurnEvent.chat_message_id.asc(), ChatTurnEvent.phase_sequence.asc())
        )
        rows = list(self.db.execute(query).scalars().all())
        grouped: Dict[int, List[ChatTurnEvent]] = {}
        for row in rows:
            message_id = int(getattr(row, "chat_message_id", 0) or 0)
            if message_id <= 0:
                continue
            grouped.setdefault(message_id, []).append(row)
        return grouped

    def _map_turn_detail_items(
        self,
        *,
        message: ChatMessage,
        turn_number: int,
        timestamp: Optional[str],
        canonical_events: List[ChatTurnEvent],
    ) -> List[ChatTranscriptItem]:
        """Map canonical reasoning/tool/observation detail items in persisted order."""
        if not canonical_events:
            return []
        return self._map_canonical_turn_events(
            message=message,
            turn_number=turn_number,
            timestamp=timestamp,
            canonical_events=canonical_events,
        )

    @staticmethod
    def _map_cancelled_tool_execution_items(
        *,
        message: ChatMessage,
        turn_number: int,
        timestamp: Optional[str],
        executions: List[ToolExecution],
        existing_tool_call_ids: set[str],
    ) -> List[ChatTranscriptItem]:
        """Project stopped tool execution rows into render-ready tool items."""
        if not executions:
            return []
        items: List[ChatTranscriptItem] = []
        sequence_seed = int(getattr(message, "id", 0) or 0) * 1000 + 900
        for index, execution in enumerate(executions):
            tool_call_id = str(getattr(execution, "tool_call_id", "") or "").strip()
            if not tool_call_id or tool_call_id in existing_tool_call_ids:
                continue
            metadata_source = (
                getattr(execution, "execution_metadata", None)
                if isinstance(getattr(execution, "execution_metadata", None), dict)
                else {}
            )
            cancellation = metadata_source.get("cancellation")
            cancellation = cancellation if isinstance(cancellation, dict) else {}
            tool_name = str(getattr(execution, "tool_name", "") or "").strip() or "unknown"
            phase_sequence = sequence_seed + index
            metadata: Dict[str, Any] = {
                "message_id": getattr(message, "id", None),
                "conversation_id": getattr(message, "conversation_id", None),
                "turn_id": getattr(execution, "turn_id", None),
                "id": getattr(execution, "turn_id", None),
                "ind": 1,
                "sequence": phase_sequence,
                "sequence_authority": "tool_execution_cancel_projection",
                "phase_sequence": phase_sequence,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "status": "cancelled",
                "failure_category": "user_cancelled",
                "cancellation_source": "chat_stop",
                "process_state": cancellation.get("process_state") or "orphaned_until_terminal",
                "runtime_kill_attempted": bool(cancellation.get("runtime_kill_attempted")),
                "runtime_kill_supported": bool(cancellation.get("runtime_kill_supported")),
            }
            tool_batch_id = metadata_source.get("tool_batch_id")
            if isinstance(tool_batch_id, str) and tool_batch_id.strip():
                metadata["tool_batch_id"] = tool_batch_id.strip()
            if timestamp:
                metadata["timestamp"] = timestamp
            items.append(
                ChatTranscriptItem(
                    id=f"tool-execution-{getattr(execution, 'id', tool_call_id)}",
                    kind="tool",
                    turn_number=turn_number,
                    content="Tool stopped",
                    metadata={key: value for key, value in metadata.items() if value is not None},
                )
            )
        return items

    def _map_canonical_turn_events(
        self,
        *,
        message: ChatMessage,
        turn_number: int,
        timestamp: Optional[str],
        canonical_events: List[ChatTurnEvent],
    ) -> List[ChatTranscriptItem]:
        """Map canonical persisted turn events into transcript detail items.

        Reasoning rows use ``ind=0`` (reasoning phase) to match the live
        streaming contract. Tool and observation rows continue to use
        ``ind=1`` (execution phase).
        """
        items: List[ChatTranscriptItem] = []
        for event in canonical_events:
            kind = str(getattr(event, "kind", "") or "").strip().lower()
            if kind not in _DETAIL_KINDS:
                continue
            phase_sequence = int(getattr(event, "phase_sequence", 0) or 0)
            metadata = dict(getattr(event, "event_metadata", None) or {})
            metadata["message_id"] = getattr(message, "id", None)
            metadata["sequence"] = phase_sequence
            metadata["sequence_authority"] = "canonical_detail"
            metadata["phase_sequence"] = phase_sequence
            # Reasoning events use ind=0 (reasoning phase), matching the live
            # streaming contract; tool/observation events use ind=1 (execution).
            metadata.setdefault("ind", 0 if kind == "reasoning" else 1)
            if kind == "reasoning":
                metadata.setdefault(
                    "reasoning_section_id",
                    f"msg-{getattr(message, 'id', 0)}-reasoning-{phase_sequence}",
                )
            if getattr(event, "sub_turn_index", None) is not None:
                metadata.setdefault("sub_turn_index", getattr(event, "sub_turn_index"))
            if kind == "tool" and getattr(event, "tool_call_id", None):
                metadata.setdefault("tool_call_id", getattr(event, "tool_call_id"))
            if timestamp:
                metadata.setdefault("timestamp", timestamp)
            item_id = str(
                getattr(event, "tool_call_id", None)
                or f"msg-{getattr(message, 'id', 0)}-{kind}-{phase_sequence}"
            )
            items.append(
                ChatTranscriptItem(
                    id=item_id,
                    kind=kind,  # type: ignore[arg-type]
                    turn_number=turn_number,
                    content=str(getattr(event, "content", "") or ""),
                    metadata=metadata,
                )
            )
        return items


    @staticmethod
    def _serialize_timestamp(value: Any) -> Optional[str]:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str):
            candidate = value.strip()
            if candidate:
                return candidate
        return None

    @staticmethod
    def _message_kind(message_type: str) -> TranscriptKind:
        """Map persisted message type into transcript item kind."""
        if message_type in _USER_MESSAGE_TYPES:
            return "user"
        if message_type in _ASSISTANT_MESSAGE_TYPES:
            return "assistant"
        if message_type.startswith("reasoning"):
            return "reasoning"
        if message_type.startswith("observation"):
            return "observation"
        if message_type.startswith("tool"):
            return "tool"
        return "assistant"
