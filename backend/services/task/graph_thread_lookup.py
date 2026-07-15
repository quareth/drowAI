"""Task graph identity lookup helpers.

This module keeps database access for `tasks.graph_thread_id` out of LangGraph
configuration code while preserving fail-closed SaaS runtime behavior.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import Task
from backend.services.langgraph_chat.checkpoint.thread_identity import require_graph_thread_id


def load_task_graph_thread_id(db: Session, *, task_id: int) -> str:
    """Return the immutable graph identity for a task or fail closed."""
    graph_thread_id = db.execute(
        select(Task.graph_thread_id).where(Task.id == int(task_id))
    ).scalar_one_or_none()
    return require_graph_thread_id(graph_thread_id, task_id=int(task_id))


__all__ = ["load_task_graph_thread_id"]
