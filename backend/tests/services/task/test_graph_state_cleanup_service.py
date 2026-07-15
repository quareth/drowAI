"""Tests for task-owned LangGraph state cleanup."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.services.task.graph_state_cleanup_service import TaskGraphStateCleanupService

GRAPH_THREAD_ID = "a" * 32
GRAPH_THREAD = f"graph-{GRAPH_THREAD_ID}"
LEGACY_THREAD = "task-7"
OTHER_THREAD = "graph-" + ("b" * 32)


class _FakeCheckpointerService:
    def __init__(self) -> None:
        self.invalidated: list[int] = []

    async def invalidate_task(self, task_id: int) -> None:
        self.invalidated.append(task_id)


def _session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    return Session(bind=engine)


def _scalar(db: Session, sql: str, params: dict[str, Any] | None = None) -> int:
    return int(db.execute(text(sql), params or {}).scalar_one())


@pytest.mark.asyncio
async def test_cleanup_deletes_current_and_legacy_checkpoint_threads() -> None:
    db = _session()
    fake_checkpointer = _FakeCheckpointerService()
    service = TaskGraphStateCleanupService(db, checkpointer_service=fake_checkpointer)  # type: ignore[arg-type]

    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        db.execute(text(f"CREATE TABLE {table} (thread_id TEXT NOT NULL, payload TEXT)"))
        db.execute(text(f"INSERT INTO {table} (thread_id, payload) VALUES (:thread_id, 'graph')"), {"thread_id": GRAPH_THREAD})
        db.execute(text(f"INSERT INTO {table} (thread_id, payload) VALUES (:thread_id, 'legacy')"), {"thread_id": LEGACY_THREAD})
        db.execute(text(f"INSERT INTO {table} (thread_id, payload) VALUES (:thread_id, 'other')"), {"thread_id": OTHER_THREAD})

    for table in ("turn_workflows", "interrupt_tickets", "task_turn_counter"):
        db.execute(text(f"CREATE TABLE {table} (task_id INTEGER NOT NULL, payload TEXT)"))
        db.execute(text(f"INSERT INTO {table} (task_id, payload) VALUES (7, 'owned')"))
        db.execute(text(f"INSERT INTO {table} (task_id, payload) VALUES (8, 'other')"))
    db.commit()

    await service.cleanup_task_graph_state(task_id=7, graph_thread_id=GRAPH_THREAD_ID)

    for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        assert _scalar(db, f"SELECT COUNT(*) FROM {table}") == 1
        assert _scalar(
            db,
            f"SELECT COUNT(*) FROM {table} WHERE thread_id = :thread_id",
            {"thread_id": OTHER_THREAD},
        ) == 1

    for table in ("turn_workflows", "interrupt_tickets", "task_turn_counter"):
        assert _scalar(db, f"SELECT COUNT(*) FROM {table}") == 1
        assert _scalar(db, f"SELECT COUNT(*) FROM {table} WHERE task_id = 8") == 1

    assert fake_checkpointer.invalidated == [7]


@pytest.mark.asyncio
async def test_cleanup_is_safe_when_checkpoint_tables_are_absent() -> None:
    db = _session()
    fake_checkpointer = _FakeCheckpointerService()
    service = TaskGraphStateCleanupService(db, checkpointer_service=fake_checkpointer)  # type: ignore[arg-type]

    await service.cleanup_task_graph_state(task_id=7, graph_thread_id=GRAPH_THREAD_ID)

    assert fake_checkpointer.invalidated == [7]


@pytest.mark.asyncio
async def test_cleanup_preserves_legacy_deletion_when_graph_id_is_invalid() -> None:
    db = _session()
    fake_checkpointer = _FakeCheckpointerService()
    service = TaskGraphStateCleanupService(db, checkpointer_service=fake_checkpointer)  # type: ignore[arg-type]

    db.execute(text("CREATE TABLE checkpoints (thread_id TEXT NOT NULL, payload TEXT)"))
    db.execute(text("INSERT INTO checkpoints (thread_id, payload) VALUES (:thread_id, 'legacy')"), {"thread_id": LEGACY_THREAD})
    db.execute(text("INSERT INTO checkpoints (thread_id, payload) VALUES (:thread_id, 'other')"), {"thread_id": OTHER_THREAD})
    db.commit()

    await service.cleanup_task_graph_state(task_id=7, graph_thread_id="")

    assert _scalar(db, "SELECT COUNT(*) FROM checkpoints") == 1
    assert _scalar(
        db,
        "SELECT COUNT(*) FROM checkpoints WHERE thread_id = :thread_id",
        {"thread_id": OTHER_THREAD},
    ) == 1
    assert fake_checkpointer.invalidated == [7]
