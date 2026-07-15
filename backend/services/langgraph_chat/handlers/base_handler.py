"""
Base handler interface for LangGraph execution branches.

Responsibilities:
- Define handler contract (handle method)
- Share common dependencies (services, adapters)

Out of scope:
- Branch selection logic (handled by selectors)
- Feature flag enforcement (handled by facade orchestration)
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..checkpoint.checkpointer_service import CheckpointerService
    from ..contracts import LangGraphChatResult, LangGraphRuntimeConfig
    from backend.services.langgraph_chat.execution.graph_executor import LangGraphExecutor
    from backend.services.langgraph_chat.streaming.adapter import LangGraphStreamingAdapter


class BaseLangGraphHandler(ABC):
    """Base class for LangGraph execution branch handlers."""

    def __init__(
        self,
        checkpointer_service: "CheckpointerService",
        executor: "LangGraphExecutor",
        streaming_adapter: "LangGraphStreamingAdapter",
    ) -> None:
        """Initialize handler with shared services.
        
        Args:
            checkpointer_service: Service for managing checkpointer lifecycle
            executor: Service for executing graphs
            streaming_adapter: Adapter for processing streaming events
        """
        self._checkpointer = checkpointer_service
        self._executor = executor
        self._adapter = streaming_adapter

    @abstractmethod
    async def handle(
        self, runtime_config: "LangGraphRuntimeConfig"
    ) -> "LangGraphChatResult":
        """Execute this branch and return result.
        
        Args:
            runtime_config: Runtime configuration with chat inputs and metadata
            
        Returns:
            LangGraphChatResult with final text, events, and state
        """
        pass

    def _build_cancellation_checker(self, task_id: int, turn_id: str) -> Callable[[], bool]:
        """Build explicit lifecycle cancellation checker for completion callbacks."""
        from backend.services.langgraph_chat.runtime.run_lifecycle import get_run_lifecycle_service

        lifecycle = get_run_lifecycle_service()
        last_checked_at = 0.0
        cached_cancel = False
        poll_interval_seconds = 0.25

        def should_cancel() -> bool:
            nonlocal last_checked_at, cached_cancel
            if cached_cancel:
                return True
            now = time.monotonic()
            if now - last_checked_at < poll_interval_seconds:
                return False
            last_checked_at = now
            cached_cancel = lifecycle.is_cancel_requested(task_id=task_id, turn_id=turn_id)
            return cached_cancel

        return should_cancel


__all__ = ["BaseLangGraphHandler"]
