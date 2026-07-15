"""Resolve durable LangGraph checkpoint anchors for task-local rewind operations.

This module owns the small, read-only checkpointer lookup needed by retry,
stop, and future checkpoint-rewind consumers. It does not mutate workflow
state, delete checkpoints, or execute graph continuations; callers use the
returned anchor as durable identity for their own operation-specific rules.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from backend.database import SessionLocal
from backend.services.langgraph_chat.checkpoint.checkpointer_service import (
    CheckpointerService,
    get_shared_checkpointer_service,
)
from backend.services.langgraph_chat.checkpoint.thread_identity import format_graph_thread_id
from backend.services.langgraph_chat.hitl_constants import (
    DEFAULT_GRAPH_NAME,
    GRAPH_NAME_DEEP_REASONING,
)
from backend.services.task.graph_thread_lookup import load_task_graph_thread_id

logger = logging.getLogger("backend.services.langgraph_chat.checkpoint_anchor_service")


@dataclass(frozen=True)
class CheckpointAnchor:
    """Stable checkpoint identity resolved from a LangGraph state snapshot."""

    task_id: int
    graph_name: str
    checkpoint_id: str
    thread_id: str


def _normalize_graph_name(graph_name: Optional[str]) -> Optional[str]:
    if not isinstance(graph_name, str):
        return None
    cleaned = graph_name.strip()
    if not cleaned:
        return None
    if cleaned == "simple_tool_execution":
        return DEFAULT_GRAPH_NAME
    return cleaned


def _candidate_graph_names(graph_name: Optional[str]) -> tuple[str, ...]:
    normalized = _normalize_graph_name(graph_name)
    if normalized:
        return (normalized,)
    return (DEFAULT_GRAPH_NAME, GRAPH_NAME_DEEP_REASONING)


def _extract_checkpoint_id(state_snapshot: Any) -> Optional[str]:
    config = getattr(state_snapshot, "config", None)
    if not isinstance(config, dict):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, dict):
        return None
    checkpoint_id = configurable.get("checkpoint_id")
    if checkpoint_id is None:
        return None
    cleaned = str(checkpoint_id).strip()
    return cleaned or None


class CheckpointAnchorService:
    """Resolve the latest checkpoint anchor for one task and graph."""

    def __init__(
        self,
        checkpointer_service: Optional[CheckpointerService] = None,
    ) -> None:
        self._checkpointer = checkpointer_service or get_shared_checkpointer_service()

    async def resolve_latest_anchor(
        self,
        *,
        task_id: int,
        graph_name: Optional[str] = None,
    ) -> Optional[CheckpointAnchor]:
        """Return the latest checkpoint anchor, or ``None`` when unavailable."""
        if not isinstance(task_id, int) or task_id <= 0:
            return None

        for candidate_graph_name in _candidate_graph_names(graph_name):
            anchor = await self._resolve_graph_anchor(
                task_id=task_id,
                graph_name=candidate_graph_name,
            )
            if anchor is not None:
                return anchor
        return None

    async def _resolve_graph_anchor(
        self,
        *,
        task_id: int,
        graph_name: str,
    ) -> Optional[CheckpointAnchor]:
        from agent.graph.builders.deep_reasoning_builder import (
            compile_deep_reasoning_graph,
        )
        from agent.graph.builders.simple_tool_builder import build_simple_tool_graph

        thread_id = format_graph_thread_id(
            _load_graph_thread_id(task_id=task_id),
            task_id=task_id,
        )
        config = {
            "configurable": {
                "thread_id": thread_id,
                "graph_name": graph_name,
            }
        }

        try:
            async with self._checkpointer.get_checkpointer(task_id) as checkpointer:
                if graph_name == GRAPH_NAME_DEEP_REASONING:
                    compiled = compile_deep_reasoning_graph(checkpointer=checkpointer)
                else:
                    compiled = build_simple_tool_graph(checkpointer=checkpointer)
                state_snapshot = await compiled.aget_state(config)
        except Exception:
            logger.debug(
                "Failed to resolve checkpoint anchor (task=%s graph=%s)",
                task_id,
                graph_name,
                exc_info=True,
            )
            return None

        checkpoint_id = _extract_checkpoint_id(state_snapshot)
        if checkpoint_id is None:
            logger.debug(
                "No checkpoint anchor found (task=%s graph=%s)",
                task_id,
                graph_name,
            )
            return None

        return CheckpointAnchor(
            task_id=task_id,
            graph_name=graph_name,
            checkpoint_id=checkpoint_id,
            thread_id=thread_id,
        )


async def resolve_latest_checkpoint_anchor_best_effort(
    *,
    task_id: int,
    graph_name: Optional[str] = None,
    checkpointer_service: Optional[CheckpointerService] = None,
) -> Optional[CheckpointAnchor]:
    """Best-effort helper for callers that only need a one-off anchor lookup."""
    service = CheckpointAnchorService(checkpointer_service=checkpointer_service)
    return await service.resolve_latest_anchor(task_id=task_id, graph_name=graph_name)


def _load_graph_thread_id(*, task_id: int) -> str:
    db = SessionLocal()
    try:
        return load_task_graph_thread_id(db, task_id=task_id)
    finally:
        db.close()


__all__ = [
    "CheckpointAnchor",
    "CheckpointAnchorService",
    "resolve_latest_checkpoint_anchor_best_effort",
]
