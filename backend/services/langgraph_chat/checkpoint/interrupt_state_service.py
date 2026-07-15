"""
Interrupt state service for HITL snapshot hydration.

Queries LangGraph checkpointer directly and normalizes interrupt metadata into a
stable API contract for backend callers and frontend snapshot fetches.

Responsibilities:
- Query checkpointer for pending interrupt state
- Normalize payload shape (including stable interrupt identifiers)
- Provide a clean API for frontend and backend components

Out of scope:
- Storing interrupt state (checkpointer handles this)
- Graph execution (handled by executor)
- Resume logic (handled by facade)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from backend.services.langgraph_chat.checkpoint.checkpointer_service import (
    CheckpointerService,
)
from backend.services.langgraph_chat.hitl_constants import (
    DEFAULT_GRAPH_NAME,
    GRAPH_NAME_DEEP_REASONING,
)
from backend.services.langgraph_chat.checkpoint.thread_identity import format_graph_thread_id

logger = logging.getLogger("backend.services.langgraph_chat.interrupt_state_service")


class InterruptStateService:
    """Query and normalize interrupt state from LangGraph checkpointer."""

    def __init__(
        self,
        checkpointer_service: Optional[CheckpointerService] = None,
    ) -> None:
        """Initialize service with checkpointer dependency.

        Args:
            checkpointer_service: Service for checkpointer lifecycle management.
                                  If None, creates default instance.
        """
        self._checkpointer = checkpointer_service or CheckpointerService()

    async def get_pending_interrupt(
        self,
        task_id: int,
        graph_name: Optional[str] = None,
        graph_thread_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Get pending interrupt for a task from checkpointer.

        Queries the LangGraph checkpointer to retrieve current graph state
        and extracts any pending interrupt payload.

        Args:
            task_id: Task ID to check for pending interrupt.
            graph_name: Which graph to query. If None, checks BOTH graphs
                       (simple_tool first, then deep_reasoning).

        Returns:
            Dict with interrupt details if pending, None otherwise.
            Format: {
                "task_id": int,
                "thread_id": str,
                "graph_name": str,
                "interrupt_type": str,
                "payload": dict,
                "resumable": bool,
            }
        """
        # If specific graph requested, check only that one
        if graph_name is not None:
            return await self._check_graph_for_interrupt(
                task_id,
                graph_name,
                graph_thread_id=graph_thread_id,
                thread_id=thread_id,
            )

        # Otherwise check BOTH graphs - simple_tool first, then deep_reasoning
        # This ensures we find interrupts regardless of which graph created them
        for gname in [DEFAULT_GRAPH_NAME, GRAPH_NAME_DEEP_REASONING]:
            result = await self._check_graph_for_interrupt(
                task_id,
                gname,
                graph_thread_id=graph_thread_id,
                thread_id=thread_id,
            )
            if result is not None:
                return result

        return None

    async def _check_graph_for_interrupt(
        self,
        task_id: int,
        graph_name: str,
        graph_thread_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Check a specific graph for pending interrupt.

        Internal helper to query one graph's checkpointer for interrupt state.

        Args:
            task_id: Task ID to check.
            graph_name: Graph name to query (simple_tool or deep_reasoning).

        Returns:
            Interrupt details if found, None otherwise.
        """
        # Import graph builders here to avoid circular imports
        from agent.graph.builders.simple_tool_builder import build_simple_tool_graph
        from agent.graph.builders.deep_reasoning_builder import (
            compile_deep_reasoning_graph,
        )

        resolved_thread_id = (
            thread_id.strip()
            if isinstance(thread_id, str) and thread_id.strip()
            else format_graph_thread_id(graph_thread_id, task_id=task_id)
        )
        config = {"configurable": {"thread_id": resolved_thread_id}}

        try:
            async with self._checkpointer.get_checkpointer(task_id) as checkpointer:
                # Build graph with checkpointer to enable state query
                if graph_name == GRAPH_NAME_DEEP_REASONING:
                    compiled = compile_deep_reasoning_graph(checkpointer=checkpointer)
                else:
                    compiled = build_simple_tool_graph(checkpointer=checkpointer)

                # Query current state from checkpointer
                state_snapshot = await compiled.aget_state(config)

                if not state_snapshot:
                    logger.debug(
                        "[INTERRUPT_SERVICE] No state found for task %s, graph=%s",
                        task_id,
                        graph_name,
                    )
                    return None

                # Check for pending interrupt in tasks
                checkpoint_id = None
                config = getattr(state_snapshot, "config", None)
                if isinstance(config, dict):
                    configurable = config.get("configurable")
                    if isinstance(configurable, dict):
                        checkpoint_id = configurable.get("checkpoint_id")
                checkpoint_id_str = (
                    str(checkpoint_id) if checkpoint_id is not None else None
                )

                # LangGraph stores interrupts in state_snapshot.tasks
                if hasattr(state_snapshot, "tasks") and state_snapshot.tasks:
                    for task in state_snapshot.tasks:
                        if hasattr(task, "interrupts") and task.interrupts:
                            interrupt_data = task.interrupts[0]
                            # Extract payload - handle both Interrupt objects and raw dicts
                            raw_payload = (
                                interrupt_data.value
                                if hasattr(interrupt_data, "value")
                                else interrupt_data
                            )
                            payload = (
                                dict(raw_payload)
                                if isinstance(raw_payload, dict)
                                else {}
                            )
                            resumable = getattr(interrupt_data, "resumable", True)
                            interrupt_id = self._resolve_interrupt_id(
                                task_id=task_id,
                                graph_name=graph_name,
                                payload=payload,
                                checkpoint_id=checkpoint_id_str,
                            )
                            payload.setdefault("interrupt_id", interrupt_id)
                            interrupt_type = payload.get("type", "tool_approval")
                            if (
                                not isinstance(interrupt_type, str)
                                or not interrupt_type.strip()
                            ):
                                interrupt_type = "tool_approval"

                            logger.info(
                                "[INTERRUPT_SERVICE] Found pending interrupt for task %s, "
                                "graph=%s, type=%s",
                                task_id,
                                graph_name,
                                interrupt_type,
                            )

                            result = {
                                "task_id": task_id,
                                "thread_id": resolved_thread_id,
                                "graph_name": graph_name,
                                "checkpoint_id": checkpoint_id_str,
                                "interrupt_id": interrupt_id,
                                "interrupt_type": interrupt_type,
                                "payload": payload,
                                "resumable": resumable,
                            }
                            reserved_id = payload.get("reserved_message_id")
                            if isinstance(reserved_id, int):
                                result["reserved_message_id"] = reserved_id
                            return result

                logger.debug(
                    "[INTERRUPT_SERVICE] No pending interrupt for task %s, graph=%s",
                    task_id,
                    graph_name,
                )
                return None

        except Exception as exc:
            logger.warning(
                "[INTERRUPT_SERVICE] Failed to query interrupt for task %s, graph=%s: %s",
                task_id,
                graph_name,
                exc,
            )
            # Return None rather than raising - allows graceful degradation
            # Caller can decide whether to treat this as "no interrupt" or error
            return None

    async def has_pending_interrupt(
        self,
        task_id: int,
        graph_name: Optional[str] = None,
        graph_thread_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> bool:
        """Check if task has a pending interrupt.

        Convenience method that returns boolean instead of full payload.

        Args:
            task_id: Task ID to check.
            graph_name: Which graph to query. If None, checks both graphs.

        Returns:
            True if interrupt is pending, False otherwise.
        """
        result = await self.get_pending_interrupt(
            task_id,
            graph_name,
            graph_thread_id=graph_thread_id,
            thread_id=thread_id,
        )
        return result is not None

    @staticmethod
    def _resolve_interrupt_id(
        *,
        task_id: int,
        graph_name: str,
        payload: Dict[str, Any],
        checkpoint_id: Optional[str],
    ) -> str:
        candidate = payload.get("interrupt_id")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

        if isinstance(checkpoint_id, str) and checkpoint_id.strip():
            return f"{graph_name}:checkpoint:{checkpoint_id.strip()}"

        turn_id = payload.get("turn_id")
        if isinstance(turn_id, str) and turn_id.strip():
            return f"{graph_name}:turn:{turn_id.strip()}"

        turn_sequence = payload.get("turn_sequence")
        if isinstance(turn_sequence, int) and not isinstance(turn_sequence, bool):
            return f"{graph_name}:turn-seq:{turn_sequence}"

        return f"{graph_name}:task:{task_id}:legacy"


# Singleton instance for convenience
_interrupt_service: Optional[InterruptStateService] = None


def get_interrupt_state_service() -> InterruptStateService:
    """Get singleton interrupt state service instance.

    Returns:
        Shared InterruptStateService instance.
    """
    global _interrupt_service
    if _interrupt_service is None:
        _interrupt_service = InterruptStateService()
    return _interrupt_service


__all__ = [
    "InterruptStateService",
    "get_interrupt_state_service",
]
