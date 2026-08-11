"""Project closed shell sessions into chat history and live task streams.

This module owns projection of an already-decided terminal close fact into the
canonical chat turn-event row and its matching live `tool_end` packet. It does
not own PTY/session mechanics, shell registries, runtime providers, graph
execution, frontend parsing, or production singleton composition.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from backend.core.logging import safe_identifier_fingerprint
from backend.services.chat.turn_event_service import ChatTurnEventService
from backend.services.streaming.stream_event_schema import STEP_TOOL_END, TOOL_PHASE_INDEX
from backend.services.terminal.contracts import (
    ShellSessionLifecycleProjectorPort,
    ShellSessionTerminalEvent,
)
from runtime_shared.shell_session_contracts import (
    ShellInteractionBoundary,
    ShellProcessStatus,
    ShellSessionLifecycleStatus,
)

logger = logging.getLogger(__name__)

_TERMINAL_LIFECYCLE_CLOSE_REASONS = frozenset(
    {
        "cancelled",
        "deadline_expired",
        "idle_expired",
        "interrupted",
        "operation_failed",
        "owner_cleanup",
        "task_cleanup",
    }
)


class ShellSessionLifecycleProjector(ShellSessionLifecycleProjectorPort):
    """Persist and publish eligible shell terminal close lifecycle events."""

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        stream_hub_provider: Callable[[], Any],
        wall_clock: Callable[[], float],
    ) -> None:
        self._session_factory = session_factory
        self._stream_hub_provider = stream_hub_provider
        self._wall_clock = wall_clock

    async def project_terminal_event(self, event: ShellSessionTerminalEvent) -> None:
        """Project one terminal event when it belongs to a main chat turn."""
        event_metadata = self._persist_terminal_lifecycle_event(event)
        if event_metadata is not None:
            await self._publish_terminal_lifecycle_event(
                event=event,
                event_metadata=event_metadata,
            )

    def _persist_terminal_lifecycle_event(
        self,
        event: ShellSessionTerminalEvent,
    ) -> dict[str, Any] | None:
        if event.close_reason not in _TERMINAL_LIFECYCLE_CLOSE_REASONS:
            return None
        turn_id = self._turn_id_from_execution_owner_id(
            event.identity.execution_owner_id
        )
        if turn_id is None:
            return None
        status, process_status = self._terminal_lifecycle_status(event.close_reason)
        try:
            db = self._session_factory()
            try:
                tool_call_id = self._terminal_lifecycle_tool_call_id(event)
                tool_name = self._terminal_lifecycle_tool_name(event)
                tool_batch_id = self._terminal_lifecycle_tool_batch_id(event)
                created = ChatTurnEventService(db).append_terminal_tool_lifecycle_event(
                    task_id=event.identity.task_id,
                    turn_id=turn_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    content="Shell session closed",
                    status=status,
                    process_status=process_status,
                    session_status=ShellSessionLifecycleStatus.CLOSED.value,
                    interaction_boundary=ShellInteractionBoundary.TERMINAL.value,
                    session_id=event.public_session_id,
                    close_reason=event.close_reason,
                    metadata={
                        "tool_call_id": tool_call_id,
                        "tool_batch_id": tool_batch_id,
                        "tool_name": tool_name,
                        "execution_owner_id": event.identity.execution_owner_id,
                        "runtime_placement_mode": event.identity.runtime_placement_mode,
                        "runner_id": event.identity.runner_id,
                        "execution_site_id": event.identity.execution_site_id,
                        "originating_capability": event.originating_capability.value,
                        "compact_tool_result": self._terminal_lifecycle_compact_result(
                            status=status,
                            process_status=process_status,
                            session_id=event.public_session_id,
                        ),
                    },
                )
                event_metadata = (
                    dict(created.event_metadata)
                    if created is not None and isinstance(created.event_metadata, dict)
                    else None
                )
                db.commit()
                return event_metadata
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        except Exception:
            logger.debug(
                "shell_session canonical terminal lifecycle persistence failed task_id=%s owner_fp=%s close_reason=%s",
                event.identity.task_id,
                safe_identifier_fingerprint(event.identity.execution_owner_id),
                self._stable_segment(event.close_reason),
                exc_info=True,
            )
            return None

    async def _publish_terminal_lifecycle_event(
        self,
        *,
        event: ShellSessionTerminalEvent,
        event_metadata: dict[str, Any],
    ) -> None:
        if event.close_reason == "cancelled":
            return
        try:
            status, process_status = self._terminal_lifecycle_status(event.close_reason)
            tool_call_id = (
                str(
                    event_metadata.get("tool_call_id")
                    or self._terminal_lifecycle_tool_call_id(event)
                ).strip()
                or self._terminal_lifecycle_tool_call_id(event)
            )
            tool_name = str(
                event_metadata.get("tool_name") or self._terminal_lifecycle_tool_name(event)
            ).strip()
            tool_name = tool_name or self._terminal_lifecycle_tool_name(event)
            tool_batch_id = str(
                event_metadata.get("tool_batch_id")
                or self._terminal_lifecycle_tool_batch_id(event)
                or ""
            ).strip() or None
            turn_id = self._turn_id_from_execution_owner_id(
                event.identity.execution_owner_id
            )
            metadata: dict[str, Any] = {
                "subtype": "tool_end",
                "step_type": STEP_TOOL_END,
                "ind": TOOL_PHASE_INDEX,
                "tool": tool_name,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "tool_batch_id": tool_batch_id,
                "status": status,
                "process_status": process_status,
                "session_status": ShellSessionLifecycleStatus.CLOSED.value,
                "interaction_boundary": ShellInteractionBoundary.TERMINAL.value,
                "session_id": event.public_session_id,
                "close_reason": event.close_reason,
                "lifecycle_event": "shell_session_terminal",
                "output_persistence": "transient",
                "compact_tool_result": self._terminal_lifecycle_compact_result(
                    status=status,
                    process_status=process_status,
                    session_id=event.public_session_id,
                ),
                "conversation_id": event_metadata.get("conversation_id"),
                "conversationId": event_metadata.get("conversation_id"),
                "id": turn_id,
                "turn_id": turn_id,
                "turn_sequence": event_metadata.get("turn_sequence"),
                "streaming": False,
                "is_streaming": False,
                "in_progress": False,
                "source": "shell_session_cleanup",
                "timestamp": self._wall_clock(),
                "execution_owner_id": event.identity.execution_owner_id,
                "runtime_placement_mode": event.identity.runtime_placement_mode,
                "runner_id": event.identity.runner_id,
                "execution_site_id": event.identity.execution_site_id,
                "originating_capability": event.originating_capability.value,
            }
            await self._stream_hub_provider().publish(
                int(event.identity.task_id),
                {
                    "type": "tool_end",
                    "content": "Shell session closed",
                    "metadata": {
                        key: value
                        for key, value in metadata.items()
                        if value is not None
                    },
                },
            )
        except Exception:
            logger.debug(
                "shell_session terminal lifecycle live projection failed task_id=%s owner_fp=%s close_reason=%s",
                event.identity.task_id,
                safe_identifier_fingerprint(event.identity.execution_owner_id),
                self._stable_segment(event.close_reason),
                exc_info=True,
            )

    @staticmethod
    def _terminal_lifecycle_status(close_reason: str) -> tuple[str, str]:
        if close_reason == "deadline_expired":
            return "timeout", ShellProcessStatus.TIMED_OUT.value
        if close_reason == "operation_failed":
            return "failed", ShellProcessStatus.FAILED.value
        return "cancelled", ShellProcessStatus.TERMINATED.value

    @staticmethod
    def _terminal_lifecycle_tool_call_id(event: ShellSessionTerminalEvent) -> str:
        if event.origin is not None:
            normalized = str(event.origin.tool_call_id or "").strip()
            if normalized:
                return normalized
        return f"shell-session-{event.public_session_id}"

    @staticmethod
    def _terminal_lifecycle_tool_batch_id(
        event: ShellSessionTerminalEvent,
    ) -> str | None:
        if event.origin is None:
            return None
        normalized = str(event.origin.tool_batch_id or "").strip()
        return normalized or None

    @staticmethod
    def _terminal_lifecycle_tool_name(event: ShellSessionTerminalEvent) -> str:
        if event.origin is not None:
            normalized = str(event.origin.tool_name or "").strip()
            if normalized:
                return normalized
        return "shell.session"

    @staticmethod
    def _terminal_lifecycle_compact_result(
        *,
        status: str,
        process_status: str,
        session_id: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": "2.0",
            "tool": "shell.session",
            "status": status,
            "success": False,
            "exit_code": None,
            "summary": "Shell session closed",
            "key_findings": [],
            "errors": [],
            "report_recommendations": [],
            "structured_signals": [],
            "decision_evidence": [],
            "lossiness_risk": "low",
            "artifact_refs": [],
            "compression": None,
            "process_status": process_status,
            "session_status": ShellSessionLifecycleStatus.CLOSED.value,
            "interaction_boundary": ShellInteractionBoundary.TERMINAL.value,
            "session_id": session_id,
        }

    @staticmethod
    def _turn_id_from_execution_owner_id(execution_owner_id: str) -> str | None:
        normalized = str(execution_owner_id or "").strip()
        if not normalized.startswith("main:"):
            return None
        turn_id = normalized.removeprefix("main:").strip()
        return turn_id or None

    @staticmethod
    def _stable_segment(value: str) -> str:
        normalized = "".join(
            char if char.isalnum() or char == "_" else "_"
            for char in str(value).strip().lower()
        ).strip("_")
        return normalized or "unknown"


__all__ = ["ShellSessionLifecycleProjector"]
