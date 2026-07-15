"""Verify reporting storage ORM model metadata and persistence behavior."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from backend import models as backend_models
from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.reporting import EngagementReport, EngagementReportJob, TaskClosureMemo
from backend.models.tenant import Tenant


REPORTING_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    Task.__table__,
    TaskClosureMemo.__table__,
    EngagementReport.__table__,
    EngagementReportJob.__table__,
]


def _make_session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine, tables=REPORTING_TABLES)
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _seed_task(session):
    tenant = Tenant(slug=f"tenant-{uuid.uuid4().hex}", name="Tenant")
    user = User(username=f"user-{uuid.uuid4().hex}", password="hashed-password")
    session.add_all([tenant, user])
    session.flush()

    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name="Engagement",
    )
    session.add(engagement)
    session.flush()

    task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name="Task",
    )
    session.add(task)
    session.flush()
    return tenant, user, engagement, task


def test_reporting_models_define_required_tables_columns_and_indexes() -> None:
    assert Base.metadata.tables["task_closure_memos"] is TaskClosureMemo.__table__
    assert Base.metadata.tables["engagement_reports"] is EngagementReport.__table__
    assert Base.metadata.tables["engagement_report_jobs"] is EngagementReportJob.__table__

    assert set(TaskClosureMemo.__table__.columns.keys()) == {
        "id",
        "schema_version",
        "tenant_id",
        "user_id",
        "created_by_user_id",
        "engagement_id",
        "task_id",
        "version",
        "is_current",
        "status",
        "memo_mode",
        "source_watermark",
        "memo",
        "generation_metadata",
        "error_message",
        "created_at",
        "updated_at",
        "generated_at",
    }
    assert set(EngagementReport.__table__.columns.keys()) == {
        "id",
        "schema_version",
        "tenant_id",
        "user_id",
        "created_by_user_id",
        "engagement_id",
        "engagement_name_snapshot",
        "engagement_status_snapshot",
        "report_type",
        "version",
        "status",
        "is_current",
        "title",
        "sections",
        "markdown_snapshot",
        "source_task_memo_ids",
        "source_knowledge_refs",
        "source_evidence_refs",
        "generation_metadata",
        "error_message",
        "delete_scheduled_at",
        "delete_undo_until",
        "deletion_finalized_at",
        "deleted_by_user_id",
        "deletion_reason",
        "deletion_metadata",
        "deletion_original_is_current",
        "created_at",
        "updated_at",
        "generated_at",
    }
    assert set(EngagementReportJob.__table__.columns.keys()) == {
        "id",
        "schema_version",
        "tenant_id",
        "user_id",
        "requested_by_user_id",
        "engagement_id",
        "report_id",
        "report_type",
        "status",
        "generation_phase",
        "idempotency_key",
        "selected_task_memo_ids",
        "include_candidate_findings",
        "source_watermark",
        "current_section_id",
        "completed_sections",
        "total_sections",
        "next_attempt_at",
        "locked_by",
        "locked_at",
        "llm_runtime_selection",
        "attempt_count",
        "max_attempts",
        "last_error_code",
        "error_message",
        "last_error_at",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
    }

    memo_indexes = {index.name for index in TaskClosureMemo.__table__.indexes}
    report_indexes = {index.name for index in EngagementReport.__table__.indexes}
    job_indexes = {index.name for index in EngagementReportJob.__table__.indexes}

    assert {
        "ix_task_closure_memos_tenant_engagement_task",
        "ix_task_closure_memos_tenant_user_engagement_task_current",
        "ix_task_closure_memos_tenant_engagement_status",
        "ix_task_closure_memos_tenant_user_updated",
        "ux_task_closure_memos_current_ready",
        "ux_task_closure_memos_preparing",
    }.issubset(memo_indexes)
    assert {
        "ix_engagement_reports_tenant_engagement_created",
        "ix_engagement_reports_tenant_user_engagement_type_current",
        "ix_engagement_reports_tenant_user_created",
        "ix_engagement_reports_tenant_status",
        "ux_engagement_reports_current_ready",
    }.issubset(report_indexes)
    assert {
        "ix_engagement_report_jobs_tenant_engagement_created",
        "ix_engagement_report_jobs_tenant_status_created",
        "ix_engagement_report_jobs_tenant_user_status",
        "ux_engagement_report_jobs_tenant_idempotency",
    }.issubset(job_indexes)


def test_reporting_models_are_registered_on_package_export_surface() -> None:
    assert backend_models.TaskClosureMemo is TaskClosureMemo
    assert backend_models.EngagementReport is EngagementReport
    assert backend_models.EngagementReportJob is EngagementReportJob
    assert "TaskClosureMemo" in backend_models.__all__
    assert "EngagementReport" in backend_models.__all__
    assert "EngagementReportJob" in backend_models.__all__


def test_reporting_models_round_trip_json_and_guid_fields() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, task = _seed_task(session)

        memo = TaskClosureMemo(
            tenant_id=tenant.id,
            user_id=user.id,
            created_by_user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            version=1,
            is_current=True,
            status="ready",
            memo_mode="supported",
            source_watermark={"latest_turn": 4},
            memo={"summary": "Task contributed evidence."},
        )
        session.add(memo)
        session.flush()

        report = EngagementReport(
            tenant_id=tenant.id,
            user_id=user.id,
            created_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=1,
            status="ready",
            is_current=True,
            title="Engagement Report",
            sections=[{"id": "summary", "title": "Summary"}],
            markdown_snapshot="# Engagement Report",
            source_task_memo_ids=[str(memo.id)],
            source_knowledge_refs=[{"kind": "finding", "id": "finding-1"}],
            source_evidence_refs=[{"kind": "archive", "id": "evidence-1"}],
        )
        session.add(report)
        session.flush()

        job = EngagementReportJob(
            tenant_id=tenant.id,
            user_id=user.id,
            requested_by_user_id=user.id,
            engagement_id=engagement.id,
            report_id=report.id,
            report_type="pentest",
            status="queued",
            idempotency_key="job-1",
            selected_task_memo_ids=[str(memo.id)],
            include_candidate_findings=False,
            source_watermark={"memo_versions": [1]},
            completed_sections=[],
            total_sections=0,
            attempt_count=0,
            max_attempts=3,
        )
        session.add(job)
        session.commit()

        session.expunge_all()
        loaded_memo = session.get(TaskClosureMemo, memo.id)
        loaded_report = session.get(EngagementReport, report.id)
        loaded_job = session.get(EngagementReportJob, job.id)

        assert loaded_memo is not None
        assert loaded_report is not None
        assert loaded_job is not None
        assert isinstance(loaded_memo.id, uuid.UUID)
        assert loaded_memo.source_watermark == {"latest_turn": 4}
        assert loaded_memo.memo == {"summary": "Task contributed evidence."}
        assert loaded_report.sections == [{"id": "summary", "title": "Summary"}]
        assert loaded_report.source_task_memo_ids == [str(memo.id)]
        assert loaded_job.report_id == report.id
        assert loaded_job.source_watermark == {"memo_versions": [1]}

    engine.dispose()


def test_current_ready_memo_and_report_indexes_reject_duplicates() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, task = _seed_task(session)
        session.add_all(
            [
                TaskClosureMemo(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    created_by_user_id=user.id,
                    engagement_id=engagement.id,
                    task_id=task.id,
                    version=1,
                    is_current=True,
                    status="ready",
                    memo_mode="supported",
                    source_watermark={},
                    memo={},
                ),
                TaskClosureMemo(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    created_by_user_id=user.id,
                    engagement_id=engagement.id,
                    task_id=task.id,
                    version=2,
                    is_current=True,
                    status="ready",
                    memo_mode="supported",
                    source_watermark={},
                    memo={},
                ),
            ]
        )

        with pytest.raises(IntegrityError):
            session.commit()

        session.rollback()
        session.add_all(
            [
                EngagementReport(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    created_by_user_id=user.id,
                    engagement_id=engagement.id,
                    report_type="pentest",
                    version=1,
                    status="ready",
                    is_current=True,
                    title="Report 1",
                    sections=[],
                    source_task_memo_ids=[],
                    source_knowledge_refs=[],
                    source_evidence_refs=[],
                ),
                EngagementReport(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    created_by_user_id=user.id,
                    engagement_id=engagement.id,
                    report_type="pentest",
                    version=2,
                    status="ready",
                    is_current=True,
                    title="Report 2",
                    sections=[],
                    source_task_memo_ids=[],
                    source_knowledge_refs=[],
                    source_evidence_refs=[],
                ),
            ]
        )

        with pytest.raises(IntegrityError):
            session.commit()

    engine.dispose()


def test_preparing_memo_index_rejects_same_task_duplicates_only() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, task = _seed_task(session)
        second_task = Task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Second task",
        )
        session.add(second_task)
        session.flush()

        session.add_all(
            [
                TaskClosureMemo(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    created_by_user_id=user.id,
                    engagement_id=engagement.id,
                    task_id=task.id,
                    version=1,
                    is_current=False,
                    status="preparing",
                    memo_mode="supported",
                    source_watermark={},
                    memo={},
                ),
                TaskClosureMemo(
                    tenant_id=tenant.id,
                    user_id=user.id,
                    created_by_user_id=user.id,
                    engagement_id=engagement.id,
                    task_id=second_task.id,
                    version=1,
                    is_current=False,
                    status="preparing",
                    memo_mode="supported",
                    source_watermark={},
                    memo={},
                ),
            ]
        )
        session.commit()

        session.add(
            TaskClosureMemo(
                tenant_id=tenant.id,
                user_id=user.id,
                created_by_user_id=user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                version=2,
                is_current=False,
                status="preparing",
                memo_mode="supported",
                source_watermark={},
                memo={},
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()

    engine.dispose()
