"""Tests for startup rejection of active product local-placement tasks."""

from __future__ import annotations

import uuid as uuid_lib

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Task, TaskHistory, User
from backend.models.tenant import Tenant
from backend.services.task.local_placement_migration import (
    PRODUCT_LOCAL_RUNTIME_REJECTED,
    fail_closed_active_local_placement_tasks,
)


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


def _seed_owner(db: Session) -> tuple[Tenant, User]:
    tenant = Tenant(slug="tenant-one", name="Tenant One")
    user = User(username="owner", password="hashed")
    db.add_all([tenant, user])
    db.flush()
    return tenant, user


def _seed_task(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    status: str,
    runtime_placement_mode: str,
    workspace_id: str | None = None,
    container_id: str | None = None,
) -> Task:
    task = Task(
        graph_thread_id=uuid_lib.uuid4().hex,
        tenant_id=tenant_id,
        user_id=user_id,
        name=f"task-{uuid_lib.uuid4().hex[:8]}",
        status=status,
        runtime_placement_mode=runtime_placement_mode,
        workspace_id=workspace_id,
        container_id=container_id,
    )
    db.add(task)
    db.flush()
    return task


def test_product_startup_marks_active_local_placement_tasks_failed() -> None:
    db = _build_session()
    tenant, user = _seed_owner(db)
    active_local = _seed_task(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        status=TaskStatus.RUNNING.value,
        runtime_placement_mode="local",
        workspace_id="workspace-active-local",
        container_id="container-active-local",
    )
    active_runner = _seed_task(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        status=TaskStatus.RUNNING.value,
        runtime_placement_mode="runner",
        workspace_id="workspace-active-runner",
        container_id="container-active-runner",
    )
    terminal_local = _seed_task(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        status=TaskStatus.STOPPED.value,
        runtime_placement_mode="local",
        workspace_id="workspace-terminal-local",
        container_id="container-terminal-local",
    )
    db.commit()

    result = fail_closed_active_local_placement_tasks(
        db,
        deployment_profile="single_host",
    )
    db.commit()

    db.refresh(active_local)
    db.refresh(active_runner)
    db.refresh(terminal_local)
    history = db.query(TaskHistory).filter(TaskHistory.task_id == active_local.id).one()

    assert result.changed_count == 1
    assert result.task_ids == (active_local.id,)
    assert active_local.status == TaskStatus.FAILED.value
    assert active_local.failure_reason == PRODUCT_LOCAL_RUNTIME_REJECTED
    assert PRODUCT_LOCAL_RUNTIME_REJECTED in (active_local.error_message or "")
    assert "runner placement" in (active_local.error_message or "")
    assert active_local.workspace_id == "workspace-active-local"
    assert active_local.container_id == "container-active-local"
    assert active_runner.status == TaskStatus.RUNNING.value
    assert terminal_local.status == TaskStatus.STOPPED.value
    assert history.old_status == TaskStatus.RUNNING.value
    assert history.new_status == TaskStatus.FAILED.value
    assert history.change_source == "system"
    assert history.change_metadata["reason_code"] == PRODUCT_LOCAL_RUNTIME_REJECTED
    assert history.change_metadata["workspace_deleted"] is False
    assert history.change_metadata["runtime_files_deleted"] is False


def test_dev_local_startup_leaves_local_placement_tasks_executable() -> None:
    db = _build_session()
    tenant, user = _seed_owner(db)
    active_local = _seed_task(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        status=TaskStatus.RUNNING.value,
        runtime_placement_mode="local",
    )
    db.commit()

    result = fail_closed_active_local_placement_tasks(
        db,
        deployment_profile="dev_local",
    )
    db.commit()

    db.refresh(active_local)
    assert result.changed_count == 0
    assert active_local.status == TaskStatus.RUNNING.value
    assert active_local.failure_reason is None
    assert db.query(TaskHistory).filter(TaskHistory.task_id == active_local.id).count() == 0
