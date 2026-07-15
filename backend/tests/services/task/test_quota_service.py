"""Unit tests for task quota counting service.

This module validates that TaskQuotaService counts only counted-active task
statuses and scopes counts correctly by tenant and user.
"""

from __future__ import annotations

import uuid as uuid_lib

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Task, User
from backend.models.tenant import Tenant
from backend.services.task.quota_service import TaskQuotaService


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine, tables=[Tenant.__table__, User.__table__, Task.__table__])
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def _seed_tenant(db: Session, *, slug: str, name: str) -> Tenant:
    tenant = Tenant(slug=slug, name=name)
    db.add(tenant)
    db.flush()
    return tenant


def _seed_user(db: Session, *, username: str) -> User:
    user = User(username=username, password="hashed")
    db.add(user)
    db.flush()
    return user


def _seed_task(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    status: str,
) -> Task:
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


def test_count_active_for_tenant_counts_only_counted_active_statuses() -> None:
    db = _build_session()
    tenant_one = _seed_tenant(db, slug="tenant-one", name="Tenant One")
    tenant_two = _seed_tenant(db, slug="tenant-two", name="Tenant Two")
    user = _seed_user(db, username="owner")

    counted_statuses = TaskStatus.active_task_statuses()
    inactive_statuses = set(TaskStatus.get_all_statuses()) - counted_statuses

    for status in counted_statuses:
        _seed_task(db, tenant_id=tenant_one.id, user_id=user.id, status=status)

    for status in inactive_statuses:
        _seed_task(db, tenant_id=tenant_one.id, user_id=user.id, status=status)

    _seed_task(db, tenant_id=tenant_two.id, user_id=user.id, status=TaskStatus.RUNNING.value)
    db.commit()

    service = TaskQuotaService(db)
    assert service.count_active_for_tenant(tenant_one.id) == len(counted_statuses)


def test_count_active_for_user_is_scoped_to_tenant_and_user() -> None:
    db = _build_session()
    tenant_one = _seed_tenant(db, slug="tenant-one", name="Tenant One")
    tenant_two = _seed_tenant(db, slug="tenant-two", name="Tenant Two")
    user_one = _seed_user(db, username="owner-one")
    user_two = _seed_user(db, username="owner-two")

    _seed_task(db, tenant_id=tenant_one.id, user_id=user_one.id, status=TaskStatus.CREATED.value)
    _seed_task(db, tenant_id=tenant_one.id, user_id=user_one.id, status=TaskStatus.RUNNING.value)
    _seed_task(db, tenant_id=tenant_one.id, user_id=user_one.id, status=TaskStatus.STOPPED.value)
    _seed_task(db, tenant_id=tenant_one.id, user_id=user_two.id, status=TaskStatus.RUNNING.value)
    _seed_task(db, tenant_id=tenant_two.id, user_id=user_one.id, status=TaskStatus.RUNNING.value)
    db.commit()

    service = TaskQuotaService(db)
    assert service.count_active_for_user(tenant_one.id, user_one.id) == 2
    assert service.count_active_for_user(tenant_one.id, user_two.id) == 1
    assert service.count_active_for_user(tenant_two.id, user_one.id) == 1

