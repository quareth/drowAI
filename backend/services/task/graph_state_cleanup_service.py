"""Task-owned LangGraph state cleanup service.

This module owns durable graph/checkpoint cleanup for irreversible task delete.
It keeps checkpoint-table SQL out of routers and task cleanup orchestration.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.orm import Session

from backend.services.langgraph_chat.checkpoint.checkpointer_service import (
    CheckpointerService,
    get_shared_checkpointer_service,
)
from backend.services.langgraph_chat.checkpoint.thread_identity import (
    owned_checkpoint_thread_ids,
)


T = TypeVar("T")


class TaskGraphStateCleanupService:
    """Delete durable graph state owned by one task."""

    _CHECKPOINT_TABLES = (
        "checkpoint_writes",
        "checkpoint_blobs",
        "checkpoints",
    )
    _TASK_TABLES = (
        "turn_workflows",
        "interrupt_tickets",
        "task_turn_counter",
    )

    def __init__(
        self,
        db: Session,
        *,
        checkpointer_service: CheckpointerService | None = None,
    ) -> None:
        self.db = db
        self.checkpointer_service = checkpointer_service or get_shared_checkpointer_service()

    async def cleanup_task_graph_state(
        self,
        *,
        task_id: int,
        graph_thread_id: str,
    ) -> None:
        """Delete task graph rows and invalidate pooled checkpointer state."""
        self.cleanup_task_graph_state_sync(
            task_id=task_id,
            graph_thread_id=graph_thread_id,
            invalidate_checkpointer=False,
        )
        await self.checkpointer_service.invalidate_task(int(task_id))

    def cleanup_task_graph_state_sync(
        self,
        *,
        task_id: int,
        graph_thread_id: str,
        invalidate_checkpointer: bool = True,
    ) -> None:
        """Delete task graph rows for synchronous cleanup callers."""
        inspector = inspect(self.db.connection())
        thread_ids = owned_checkpoint_thread_ids(
            task_id=int(task_id),
            graph_thread_id=graph_thread_id,
        )

        for table_name in self._CHECKPOINT_TABLES:
            if not inspector.has_table(table_name):
                continue
            self.db.execute(
                text(
                    f"DELETE FROM {table_name} WHERE thread_id IN :thread_ids"
                ).bindparams(bindparam("thread_ids", expanding=True)),
                {"thread_ids": list(thread_ids)},
            )

        for table_name in self._TASK_TABLES:
            if not inspector.has_table(table_name):
                continue
            self.db.execute(
                text(f"DELETE FROM {table_name} WHERE task_id = :task_id"),
                {"task_id": int(task_id)},
            )

        if invalidate_checkpointer:
            _run_async_sync(
                lambda: self.checkpointer_service.invalidate_task(int(task_id))
            )


def _run_async_sync(
    coro_factory: Callable[[], Coroutine[Any, Any, T]],
) -> T | None:
    """Run a coroutine from sync code without assuming event-loop ownership."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_factory())
    if not loop.is_running():
        return loop.run_until_complete(coro_factory())

    result: dict[str, T] = {}
    error: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro_factory())
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error["value"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "value" in error:
        raise error["value"]
    return result.get("value")


__all__ = ["TaskGraphStateCleanupService"]
