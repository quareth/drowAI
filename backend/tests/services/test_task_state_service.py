"""Regression tests for task state history persistence behavior.

This module verifies that status transition metadata is stored through the
durable `TaskHistory.change_metadata` column used by reporting readiness.
"""

from __future__ import annotations

import uuid as uuid_lib

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Task, TaskHistory, User
from backend.models.tenant import Tenant
from backend.services.task.state_service import TaskStateService


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Task.__table__,
            TaskHistory.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def _seed_tenant(db: Session) -> Tenant:
    tenant = Tenant(slug=f"tenant-{uuid_lib.uuid4().hex[:8]}", name="Tenant")
    db.add(tenant)
    db.flush()
    return tenant


def _seed_user(db: Session) -> User:
    user = User(username=f"user-{uuid_lib.uuid4().hex[:8]}", password="hashed")
    db.add(user)
    db.flush()
    return user


def _seed_task(db: Session, *, tenant_id: int, user_id: int, status: str) -> Task:
    task = Task(
        graph_thread_id=uuid_lib.uuid4().hex,
        user_id=user_id,
        tenant_id=tenant_id,
        name=f"task-{uuid_lib.uuid4().hex[:8]}",
        status=status,
    )
    db.add(task)
    db.flush()
    return task


def test_change_task_status_persists_metadata_to_change_metadata() -> None:
    db = _build_session()
    tenant = _seed_tenant(db)
    user = _seed_user(db)
    task = _seed_task(db, tenant_id=tenant.id, user_id=user.id, status=TaskStatus.CREATED.value)
    db.commit()

    metadata = {
        "runtime_event_type": "runtime.retired",
        "runtime_event_lifecycle_outcome": "retired",
        "runner_id": "runner-one",
    }

    ok, message, history = TaskStateService(db).change_task_status(
        task_id=task.id,
        new_status=TaskStatus.QUEUED.value,
        user_id=user.id,
        reason="Queued by runner admission",
        change_source="system",
        metadata=metadata,
    )

    assert ok is True, message
    assert history is not None
    persisted = db.query(TaskHistory).filter(TaskHistory.task_id == task.id).one()
    assert persisted.change_metadata == metadata


def test_stage_and_committed_status_changes_use_same_history_metadata_field() -> None:
    db = _build_session()
    tenant = _seed_tenant(db)
    user = _seed_user(db)
    committed_task = _seed_task(db, tenant_id=tenant.id, user_id=user.id, status=TaskStatus.CREATED.value)
    staged_task = _seed_task(db, tenant_id=tenant.id, user_id=user.id, status=TaskStatus.STOPPED.value)
    db.commit()

    committed_metadata = {"path": "committed"}
    staged_metadata = {"path": "staged"}
    service = TaskStateService(db)

    ok, message, _history = service.change_task_status(
        task_id=committed_task.id,
        new_status=TaskStatus.QUEUED.value,
        user_id=user.id,
        reason="Queued by committed path",
        change_source="system",
        metadata=committed_metadata,
    )
    assert ok is True, message

    ok, message, _history = service.stage_task_status_change(
        task_id=staged_task.id,
        new_status=TaskStatus.QUEUED.value,
        user_id=user.id,
        reason="Queued by staged path",
        change_source="system",
        metadata=staged_metadata,
    )
    assert ok is True, message

    histories = {
        row.task_id: row.change_metadata
        for row in db.query(TaskHistory).filter(TaskHistory.task_id.in_([committed_task.id, staged_task.id])).all()
    }
    assert histories == {
        committed_task.id: committed_metadata,
        staged_task.id: staged_metadata,
    }
