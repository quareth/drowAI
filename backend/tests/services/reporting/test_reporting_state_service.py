"""Tests for memo input state projection from persisted reporting rows."""

from __future__ import annotations

import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.reporting import EngagementReport, EngagementReportJob, TaskClosureMemo
from backend.models.tenant import Tenant
from backend.services.reporting.reporting_state_service import (
    ReportingStateService,
    watermarks_match,
)


REPORTING_STATE_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    Task.__table__,
    TaskClosureMemo.__table__,
    EngagementReport.__table__,
    EngagementReportJob.__table__,
]


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine, tables=REPORTING_STATE_TABLES)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return factory()


def _seed_scope(db: Session, *, label: str) -> tuple[Tenant, User, Engagement, Task]:
    tenant = Tenant(slug=f"tenant-{label}-{uuid.uuid4().hex[:8]}", name=f"Tenant {label}")
    user = User(username=f"user-{label}-{uuid.uuid4().hex[:8]}", password="hashed")
    db.add_all([tenant, user])
    db.flush()

    engagement = Engagement(tenant_id=tenant.id, user_id=user.id, name=f"Engagement {label}")
    db.add(engagement)
    db.flush()

    task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name=f"Task {label}",
    )
    db.add(task)
    db.flush()
    return tenant, user, engagement, task


def _add_memo(
    db: Session,
    *,
    task: Task,
    user_id: int,
    engagement_id: int,
    version: int,
    status: str,
    is_current: bool,
    source_watermark: dict,
) -> TaskClosureMemo:
    memo = TaskClosureMemo(
        tenant_id=task.tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        task_id=task.id,
        version=version,
        is_current=is_current,
        status=status,
        memo_mode="supported",
        source_watermark=source_watermark,
        memo={"summary": f"memo-{version}-{status}"},
    )
    db.add(memo)
    db.flush()
    return memo


def test_no_memo_rows_project_not_prepared_state() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="empty")
    db.commit()

    projection = ReportingStateService(db).project_memo_input_state(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        current_source_watermark={"schema_version": 1, "empty": True, "sources": {}},
    )

    assert projection.input_state == "not_prepared"
    assert projection.current_memo is None
    assert projection.latest_attempt is None
    assert projection.latest_attempt_status is None


def test_latest_preparing_without_current_ready_projects_preparing_state() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="preparing")
    _add_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        version=1,
        status="preparing",
        is_current=False,
        source_watermark={"schema_version": 1},
    )
    db.commit()

    projection = ReportingStateService(db).project_memo_input_state(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        current_source_watermark={"schema_version": 1},
    )

    assert projection.input_state == "preparing"
    assert projection.current_memo is None
    assert projection.latest_attempt_status == "preparing"


def test_latest_failed_without_current_ready_projects_failed_state() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="failed")
    _add_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        version=1,
        status="failed",
        is_current=False,
        source_watermark={"schema_version": 1},
    )
    db.commit()

    projection = ReportingStateService(db).project_memo_input_state(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        current_source_watermark={"schema_version": 1},
    )

    assert projection.input_state == "failed"
    assert projection.current_memo is None
    assert projection.latest_attempt_status == "failed"


def test_current_ready_memo_projects_ready_when_watermark_matches() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="ready")
    watermark = {
        "schema_version": 1,
        "sources": {"chat_messages": {"latest_id": 10, "latest_turn_number": 2}},
    }
    ready_memo = _add_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        version=1,
        status="ready",
        is_current=True,
        source_watermark=watermark,
    )
    db.commit()

    projection = ReportingStateService(db).project_memo_input_state(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        current_source_watermark={"sources": watermark["sources"], "schema_version": 1},
    )

    assert projection.input_state == "ready"
    assert projection.current_memo is not None
    assert projection.current_memo.id == ready_memo.id
    assert projection.latest_attempt_status == "ready"
    assert projection.current_memo_stale is False


def test_ready_memo_becomes_stale_when_source_watermark_advances() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="stale")
    _add_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        version=1,
        status="ready",
        is_current=True,
        source_watermark={
            "schema_version": 1,
            "sources": {"chat_messages": {"latest_id": 10}},
        },
    )
    db.commit()

    projection = ReportingStateService(db).project_memo_input_state(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        current_source_watermark={
            "schema_version": 1,
            "sources": {"chat_messages": {"latest_id": 11}},
        },
    )

    assert projection.input_state == "stale"
    assert projection.current_memo is not None
    assert projection.current_memo_stale is True


def test_newer_failed_attempt_does_not_supersede_usable_ready_memo() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="failed-ready")
    watermark = {"schema_version": 1, "sources": {"tool_executions": {"latest_id": "abc"}}}
    ready_memo = _add_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        version=1,
        status="ready",
        is_current=True,
        source_watermark=watermark,
    )
    failed_attempt = _add_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        version=2,
        status="failed",
        is_current=False,
        source_watermark=watermark,
    )
    db.commit()

    projection = ReportingStateService(db).project_memo_input_state(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        current_source_watermark=watermark,
    )

    assert projection.input_state == "ready"
    assert projection.current_memo is not None
    assert projection.current_memo.id == ready_memo.id
    assert projection.latest_attempt is not None
    assert projection.latest_attempt.id == failed_attempt.id
    assert projection.latest_attempt_status == "failed"


def test_newer_preparing_attempt_does_not_supersede_stale_ready_memo() -> None:
    db = _build_session()
    tenant, user, engagement, task = _seed_scope(db, label="preparing-stale")
    stored_watermark = {"schema_version": 1, "sources": {"chat_messages": {"latest_id": 2}}}
    _add_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        version=1,
        status="ready",
        is_current=True,
        source_watermark=stored_watermark,
    )
    preparing_attempt = _add_memo(
        db,
        task=task,
        user_id=user.id,
        engagement_id=engagement.id,
        version=2,
        status="preparing",
        is_current=False,
        source_watermark=stored_watermark,
    )
    db.commit()

    projection = ReportingStateService(db).project_memo_input_state(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        current_source_watermark={
            "schema_version": 1,
            "sources": {"chat_messages": {"latest_id": 3}},
        },
    )

    assert projection.input_state == "stale"
    assert projection.current_memo is not None
    assert projection.latest_attempt is not None
    assert projection.latest_attempt.id == preparing_attempt.id
    assert projection.latest_attempt_status == "preparing"


def test_watermark_comparison_is_deterministic_for_mapping_key_order() -> None:
    assert watermarks_match(
        {"sources": {"b": {"latest_id": 2}, "a": {"latest_id": 1}}, "schema_version": 1},
        {"schema_version": 1, "sources": {"a": {"latest_id": 1}, "b": {"latest_id": 2}}},
    )
    assert not watermarks_match(
        {"schema_version": 1, "sources": {"a": {"latest_id": 1}}},
        {"schema_version": 1, "sources": {"a": {"latest_id": 2}}},
    )
