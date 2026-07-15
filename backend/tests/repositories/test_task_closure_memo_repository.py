"""Test canonical task-closure memo persistence and ownership boundaries."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Engagement, Task, User
from backend.models.reporting import TaskClosureMemo
from backend.models.tenant import Tenant
from backend.repositories.reporting.task_closure_memo_repository import (
    TaskClosureMemoRepository,
)
from backend.services.reporting.contracts import TASK_CLOSURE_MEMO_SCHEMA_VERSION


REPORTING_REPOSITORY_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    Task.__table__,
    TaskClosureMemo.__table__,
]


def _make_session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine, tables=REPORTING_REPOSITORY_TABLES)
    return engine, sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )


def _seed_scope(session, *, tenant_label: str):
    tenant = Tenant(
        slug=f"tenant-{tenant_label}-{uuid.uuid4().hex}", name=f"Tenant {tenant_label}"
    )
    user = User(
        username=f"user-{tenant_label}-{uuid.uuid4().hex}", password="hashed-password"
    )
    session.add_all([tenant, user])
    session.flush()

    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name=f"Engagement {tenant_label}",
    )
    session.add(engagement)
    session.flush()

    task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name=f"Task {tenant_label}",
    )
    session.add(task)
    session.flush()
    return tenant, user, engagement, task


def _add_memo(
    session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    task_id: int,
    version: int,
    status: str,
    is_current: bool,
) -> TaskClosureMemo:
    memo = TaskClosureMemo(
        tenant_id=tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        task_id=task_id,
        version=version,
        is_current=is_current,
        status=status,
        memo_mode="supported",
        source_watermark={"version": version},
        memo={"summary": f"memo-{version}-{status}"},
    )
    session.add(memo)
    session.flush()
    return memo


def test_current_ready_memo_is_scoped_and_ignores_latest_failed_attempt() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, task = _seed_scope(session, tenant_label="owner")
        other_tenant, other_user, other_engagement, other_task = _seed_scope(
            session, tenant_label="other"
        )

        ready_memo = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=1,
            status="ready",
            is_current=True,
        )
        latest_failed = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=2,
            status="failed",
            is_current=True,
        )
        _add_memo(
            session,
            tenant_id=other_tenant.id,
            user_id=other_user.id,
            engagement_id=other_engagement.id,
            task_id=other_task.id,
            version=1,
            status="ready",
            is_current=True,
        )

        repo = TaskClosureMemoRepository(session)

        current = repo.get_current_ready_memo(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        latest = repo.get_latest_memo_attempt(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        cross_tenant = repo.get_current_ready_memo(
            tenant_id=other_tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        assert current is not None
        assert current.id == ready_memo.id
        assert latest is not None
        assert latest.id == latest_failed.id
        assert cross_tenant is None

    engine.dispose()


def test_list_memos_for_tasks_filters_owner_engagement_and_task_ids() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, task_one = _seed_scope(
            session, tenant_label="list-owner"
        )
        task_two = Task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Second task",
        )
        session.add(task_two)
        session.flush()
        _, _, other_engagement, other_task = _seed_scope(
            session, tenant_label="list-other"
        )

        memo_one = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task_one.id,
            version=1,
            status="ready",
            is_current=True,
        )
        memo_two = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task_two.id,
            version=1,
            status="ready",
            is_current=True,
        )
        _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=other_engagement.id,
            task_id=other_task.id,
            version=1,
            status="ready",
            is_current=True,
        )
        _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task_two.id,
            version=2,
            status="failed",
            is_current=True,
        )

        repo = TaskClosureMemoRepository(session)

        current_ready = repo.list_memos_for_tasks(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_ids=[task_one.id, task_two.id],
            current_ready_only=True,
        )
        selected_task_only = repo.list_memos_for_tasks(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_ids=[task_one.id],
        )

        assert {memo.id for memo in current_ready} == {memo_one.id, memo_two.id}
        assert [memo.id for memo in selected_task_only] == [memo_one.id]
        assert (
            repo.list_memos_for_tasks(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                task_ids=[],
            )
            == []
        )

    engine.dispose()


def test_preparing_memo_lookup_and_stale_failure_are_scoped() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, task_one = _seed_scope(
            session, tenant_label="preparing-owner"
        )
        task_two = Task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Second preparing task",
        )
        session.add(task_two)
        session.flush()
        stale_time = datetime(2026, 6, 20, 8, 0, tzinfo=timezone.utc)
        other_stale_time = datetime(2026, 6, 20, 8, 1, tzinfo=timezone.utc)
        stale = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task_one.id,
            version=1,
            status="preparing",
            is_current=False,
        )
        other_task_stale = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task_two.id,
            version=1,
            status="preparing",
            is_current=False,
        )
        stale.created_at = stale_time
        stale.updated_at = stale_time
        other_task_stale.created_at = other_stale_time
        other_task_stale.updated_at = other_stale_time
        session.commit()

        repo = TaskClosureMemoRepository(session)
        preparing = repo.get_preparing_memo_attempt(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task_one.id,
        )
        failed_count = repo.mark_stale_preparing_memos_failed(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task_one.id,
            stale_before=datetime(2026, 6, 20, 9, 0, tzinfo=timezone.utc),
            error_message="stale prepare",
        )
        session.commit()

        session.refresh(stale)
        session.refresh(other_task_stale)
        assert preparing is not None
        assert preparing.id == stale.id
        assert failed_count == 1
        assert stale.status == "failed"
        assert stale.error_message == "stale prepare"
        assert other_task_stale.status == "preparing"

    engine.dispose()


def test_list_selected_current_ready_memos_scopes_and_preserves_request_order() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, task_one = _seed_scope(
            session, tenant_label="selected-owner"
        )
        task_one.status = TaskStatus.STOPPED.value
        task_two = Task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Second selected task",
            status=TaskStatus.STOPPED.value,
        )
        non_current_task = Task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Non-current selected task",
            status=TaskStatus.STOPPED.value,
        )
        failed_task = Task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Failed selected task",
            status=TaskStatus.STOPPED.value,
        )
        session.add_all([task_two, non_current_task, failed_task])
        session.flush()
        other_tenant, other_user, other_engagement, other_task = _seed_scope(
            session, tenant_label="selected-other"
        )
        other_task.status = TaskStatus.STOPPED.value

        memo_one = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task_one.id,
            version=1,
            status="ready",
            is_current=True,
        )
        memo_two = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task_two.id,
            version=1,
            status="ready",
            is_current=True,
        )
        non_current_memo = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=non_current_task.id,
            version=1,
            status="ready",
            is_current=False,
        )
        failed_memo = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=failed_task.id,
            version=1,
            status="failed",
            is_current=True,
        )
        foreign_memo = _add_memo(
            session,
            tenant_id=other_tenant.id,
            user_id=other_user.id,
            engagement_id=other_engagement.id,
            task_id=other_task.id,
            version=1,
            status="ready",
            is_current=True,
        )

        repo = TaskClosureMemoRepository(session)
        selected_ids = [
            str(memo_two.id),
            str(memo_one.id),
            str(memo_two.id),
            str(non_current_memo.id),
            str(failed_memo.id),
            str(foreign_memo.id),
            "not-a-uuid",
        ]

        selected_memos = repo.list_selected_current_ready_memos(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            selected_task_memo_ids=selected_ids,
        )

        assert [memo.id for memo in selected_memos] == [memo_two.id, memo_one.id]

    engine.dispose()


def test_get_selected_memo_tasks_returns_only_stopped_owner_tasks() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, stopped_task = _seed_scope(
            session, tenant_label="selected-task-owner"
        )
        stopped_task.status = TaskStatus.STOPPED.value
        running_task = Task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Running selected task",
            status=TaskStatus.RUNNING.value,
        )
        session.add(running_task)
        session.flush()
        other_tenant, other_user, other_engagement, other_task = _seed_scope(
            session, tenant_label="selected-task-other"
        )
        other_task.status = TaskStatus.STOPPED.value

        stopped_memo = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=stopped_task.id,
            version=1,
            status="ready",
            is_current=True,
        )
        running_memo = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=running_task.id,
            version=1,
            status="ready",
            is_current=True,
        )
        foreign_memo = _add_memo(
            session,
            tenant_id=other_tenant.id,
            user_id=other_user.id,
            engagement_id=other_engagement.id,
            task_id=other_task.id,
            version=1,
            status="ready",
            is_current=True,
        )

        repo = TaskClosureMemoRepository(session)

        selected_tasks = repo.get_selected_memo_tasks(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            selected_task_memo_ids=[
                str(running_memo.id),
                str(stopped_memo.id),
                str(foreign_memo.id),
            ],
        )

        assert [(memo_id, task.id) for memo_id, task in selected_tasks] == [
            (stopped_memo.id, stopped_task.id)
        ]

    engine.dispose()


def test_get_task_for_memo_preparation_requires_full_owner_lineage() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, task = _seed_scope(session, tenant_label="task-owner")
        other_tenant, other_user, other_engagement, other_task = _seed_scope(
            session,
            tenant_label="task-other",
        )

        repo = TaskClosureMemoRepository(session)

        scoped_task = repo.get_task_for_memo_preparation(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        assert scoped_task is not None
        assert scoped_task.id == task.id
        assert (
            repo.get_task_for_memo_preparation(
                tenant_id=other_tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                task_id=task.id,
            )
            is None
        )
        assert (
            repo.get_task_for_memo_preparation(
                tenant_id=tenant.id,
                user_id=other_user.id,
                engagement_id=engagement.id,
                task_id=task.id,
            )
            is None
        )
        assert (
            repo.get_task_for_memo_preparation(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=other_engagement.id,
                task_id=task.id,
            )
            is None
        )
        assert (
            repo.get_task_for_memo_preparation(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                task_id=other_task.id,
            )
            is None
        )

    engine.dispose()


def test_memo_attempt_writes_and_history_are_task_scoped() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, task = _seed_scope(
            session, tenant_label="write-owner"
        )
        other_tenant, other_user, other_engagement, other_task = _seed_scope(
            session,
            tenant_label="write-other",
        )
        existing = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=1,
            status="ready",
            is_current=True,
        )
        _add_memo(
            session,
            tenant_id=other_tenant.id,
            user_id=other_user.id,
            engagement_id=other_engagement.id,
            task_id=other_task.id,
            version=9,
            status="ready",
            is_current=True,
        )

        repo = TaskClosureMemoRepository(session)
        next_version = repo.next_memo_version(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        created = repo.create_memo_attempt(
            tenant_id=tenant.id,
            user_id=user.id,
            created_by_user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=next_version,
            source_watermark={"turn": 2},
        )

        fetched = repo.get_memo_by_id(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            memo_id=created.id,
        )
        history = repo.list_memo_history_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        assert next_version == 2
        assert created.schema_version == TASK_CLOSURE_MEMO_SCHEMA_VERSION
        assert created.is_current is False
        assert created.status == "preparing"
        assert created.source_watermark == {"turn": 2}
        assert fetched is not None
        assert fetched.id == created.id
        assert [memo.id for memo in history] == [created.id, existing.id]
        assert (
            repo.get_memo_by_id(
                tenant_id=other_tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                memo_id=created.id,
            )
            is None
        )
        assert (
            repo.get_memo_by_id(
                tenant_id=tenant.id,
                user_id=other_user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                memo_id=created.id,
            )
            is None
        )
        assert (
            repo.get_memo_by_id(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=other_engagement.id,
                task_id=task.id,
                memo_id=created.id,
            )
            is None
        )
        assert (
            repo.get_memo_by_id(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                task_id=other_task.id,
                memo_id=created.id,
            )
            is None
        )
        assert (
            repo.get_memo_by_id(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                memo_id="not-a-uuid",
            )
            is None
        )

    engine.dispose()


def test_ready_and_failed_updates_preserve_task_current_pointer_isolation() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, task = _seed_scope(
            session, tenant_label="pointer-owner"
        )
        other_tenant, other_user, other_engagement, other_task = _seed_scope(
            session,
            tenant_label="pointer-other",
        )
        current_ready = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=1,
            status="ready",
            is_current=True,
        )
        failed_attempt = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=2,
            status="failed",
            is_current=True,
        )
        preparing_attempt = _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=3,
            status="preparing",
            is_current=True,
        )
        other_current = _add_memo(
            session,
            tenant_id=other_tenant.id,
            user_id=other_user.id,
            engagement_id=other_engagement.id,
            task_id=other_task.id,
            version=1,
            status="ready",
            is_current=True,
        )

        repo = TaskClosureMemoRepository(session)
        failed = repo.mark_memo_failed(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            memo_id=preparing_attempt.id,
            error_message="generation failed",
            generation_metadata={"reason": "test"},
        )
        new_attempt = repo.create_memo_attempt(
            tenant_id=tenant.id,
            user_id=user.id,
            created_by_user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=4,
        )
        cleared_count = repo.clear_current_ready_memos_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        ready = repo.mark_memo_ready(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            memo_id=new_attempt.id,
            memo={"summary": "ready memo"},
            source_watermark={"turn": 4},
            generation_metadata={"provider": "test"},
            generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        assert cleared_count == 1
        assert ready is not None
        assert ready.id == new_attempt.id
        assert ready.status == "ready"
        assert ready.is_current is True
        assert ready.memo == {"summary": "ready memo"}
        assert ready.error_message is None
        assert failed is not None
        assert failed.id == preparing_attempt.id
        assert failed.status == "failed"
        assert failed.is_current is False
        assert failed.error_message == "generation failed"
        assert current_ready.is_current is False
        assert failed_attempt.is_current is True
        assert other_current.is_current is True
        assert (
            repo.mark_memo_ready(
                tenant_id=other_tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                memo_id=new_attempt.id,
                memo={},
                source_watermark={},
            )
            is None
        )
        assert (
            repo.mark_memo_failed(
                tenant_id=tenant.id,
                user_id=other_user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                memo_id=new_attempt.id,
                error_message="not owner scoped",
            )
            is None
        )

    engine.dispose()


def test_memo_repository_methods_do_not_commit_transactions() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, task = _seed_scope(
            session, tenant_label="no-commit-memo"
        )
        _add_memo(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=1,
            status="ready",
            is_current=True,
        )
        repo = TaskClosureMemoRepository(session)
        session.commit = Mock(
            side_effect=AssertionError("repository methods must not commit")
        )

        next_version = repo.next_memo_version(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        attempt = repo.create_memo_attempt(
            tenant_id=tenant.id,
            user_id=user.id,
            created_by_user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=next_version,
        )
        repo.clear_current_ready_memos_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        repo.mark_memo_ready(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            memo_id=attempt.id,
            memo={},
            source_watermark={},
        )
        repo.get_current_ready_memo(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        repo.get_latest_memo_attempt(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        repo.list_memos_for_tasks(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_ids=[task.id],
        )
        repo.list_memo_history_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        session.commit.assert_not_called()

    engine.dispose()
