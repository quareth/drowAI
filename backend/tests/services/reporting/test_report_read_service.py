"""Tests for read-only engagement report service projections."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.reporting import EngagementReport, EngagementReportJob, TaskClosureMemo
from backend.models.tenant import Tenant
from backend.services.reporting.report_read_service import ReportReadService


REPORT_READ_TABLES = [
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
    Base.metadata.create_all(bind=engine, tables=REPORT_READ_TABLES)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return factory()


def _now(offset_seconds: int = 0) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=offset_seconds)


def _seed_scope(db: Session, *, label: str) -> tuple[Tenant, User, Engagement]:
    tenant = Tenant(slug=f"tenant-{label}-{uuid.uuid4().hex[:8]}", name=f"Tenant {label}")
    user = User(username=f"user-{label}-{uuid.uuid4().hex[:8]}", password="hashed")
    db.add_all([tenant, user])
    db.flush()

    engagement = Engagement(tenant_id=tenant.id, user_id=user.id, name=f"Engagement {label}")
    db.add(engagement)
    db.flush()
    return tenant, user, engagement


def _add_report(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    report_type: str = "pentest",
    version: int = 1,
    status: str = "ready",
    is_current: bool = False,
    created_at: datetime | None = None,
    generation_metadata: dict[str, object] | None = None,
    engagement_name_snapshot: str | None = None,
) -> EngagementReport:
    timestamp = created_at or _now()
    report = EngagementReport(
        tenant_id=tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        engagement_name_snapshot=engagement_name_snapshot,
        engagement_status_snapshot="active",
        report_type=report_type,
        version=version,
        status=status,
        is_current=is_current,
        title=f"Report {version}",
        sections=[_valid_report_section()],
        markdown_snapshot=f"# Report {version}",
        source_task_memo_ids=[str(uuid.uuid4())],
        source_knowledge_refs=[
            {
                "ref": "knowledge_finding:1",
                "task_id": 1,
                "record_type": "finding",
                "authoritative": True,
            }
        ],
        source_evidence_refs=[
            {
                "ref": "evidence_archive:1",
                "task_id": 1,
                "evidence_type": "service",
                "source_tool": "nmap",
            }
        ],
        generation_metadata=generation_metadata or {"version": version},
        created_at=timestamp,
        updated_at=timestamp,
        generated_at=timestamp if status == "ready" else None,
    )
    db.add(report)
    db.flush()
    return report


def _valid_report_section() -> dict[str, object]:
    return {
        "schema_version": "1",
        "section_id": "executive_summary",
        "section_type": "summary",
        "title": "Executive Summary",
        "status": "ready",
        "content_markdown": "Full section content",
        "blocks": [],
        "source_refs": {
            "task_memo_ids": [],
            "knowledge_refs": [],
            "evidence_refs": [],
        },
        "unsupported_notes": [],
        "generation_notes": [],
    }


def _add_job(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    report_id: uuid.UUID | None = None,
    status: str = "queued",
) -> EngagementReportJob:
    timestamp = _now()
    job = EngagementReportJob(
        tenant_id=tenant_id,
        user_id=user_id,
        requested_by_user_id=user_id,
        engagement_id=engagement_id,
        report_id=report_id,
        report_type="pentest",
        status=status,
        idempotency_key=f"job-{uuid.uuid4()}",
        selected_task_memo_ids=[str(uuid.uuid4())],
        include_candidate_findings=True,
        source_watermark={"schema_version": 1},
        current_section_id="summary",
        completed_sections=["intro"],
        total_sections=3,
        attempt_count=0,
        max_attempts=3,
        created_at=timestamp,
        updated_at=timestamp,
        started_at=timestamp if status != "queued" else None,
        finished_at=timestamp if status in {"ready", "failed", "cancelled"} else None,
    )
    db.add(job)
    db.flush()
    return job


def test_unknown_report_type_is_rejected_before_repository_access() -> None:
    service = ReportReadService(Mock(spec=Session))
    service._report_repository = Mock()  # type: ignore[attr-defined]
    service._job_repository = Mock()  # type: ignore[attr-defined]

    with pytest.raises(ValueError):
        service.get_current_report(
            tenant_id=1,
            user_id=2,
            engagement_id=3,
            report_type="unsupported",
        )

    with pytest.raises(ValueError):
        service.list_report_history(
            tenant_id=1,
            user_id=2,
            engagement_id=3,
            report_type="unsupported",
        )

    service._report_repository.assert_not_called()  # type: ignore[attr-defined]
    service._job_repository.assert_not_called()  # type: ignore[attr-defined]


def test_empty_current_and_history_return_stable_shapes_without_jobs() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="empty")
    db.commit()

    service = ReportReadService(db)
    current = service.get_current_report(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
    )
    history = service.list_report_history(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
    )

    assert current.engagement_id == engagement.id
    assert current.report_type == "pentest"
    assert current.report is None
    assert history.engagement_id == engagement.id
    assert history.report_type == "pentest"
    assert history.reports == []
    assert db.query(EngagementReportJob).count() == 0


def test_current_report_and_history_are_scoped_read_only_shapes() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="owner")
    other_tenant, other_user, other_engagement = _seed_scope(db, label="other")
    old_report = _add_report(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        version=1,
        is_current=False,
        created_at=_now(1),
    )
    current_report = _add_report(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        version=2,
        is_current=True,
        created_at=_now(2),
    )
    _add_report(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        report_type="vulnerability_assessment",
        version=1,
        is_current=True,
        created_at=_now(3),
    )
    _add_report(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        version=3,
        status="failed",
        is_current=False,
        created_at=_now(4),
    )
    _add_report(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        version=4,
        status="generating",
        is_current=False,
        created_at=_now(5),
    )
    _add_report(
        db,
        tenant_id=other_tenant.id,
        user_id=other_user.id,
        engagement_id=other_engagement.id,
        version=1,
        is_current=True,
        created_at=_now(6),
    )
    db.commit()

    service = ReportReadService(db)
    current = service.get_current_report(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
    )
    history = service.list_report_history(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        report_type="pentest",
    )

    assert current.report is not None
    assert current.report.id == current_report.id
    assert current.report.status == "ready"
    assert current.report.sections[0].content_markdown == "Full section content"
    assert current.report.markdown_snapshot == "# Report 2"
    assert [item.report_id for item in history.reports] == [current_report.id, old_report.id]
    assert all(item.report_type == "pentest" for item in history.reports)
    assert all("sections" not in item.model_fields_set for item in history.reports)
    assert all("markdown_snapshot" not in item.model_fields_set for item in history.reports)
    assert all(item.source_task_memo_ids for item in history.reports)
    assert db.query(EngagementReportJob).count() == 0


def test_report_library_lists_owned_ready_reports_without_live_engagement_join() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="library")
    _other_tenant, other_user, other_engagement = _seed_scope(db, label="library-other")
    report = _add_report(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        version=1,
        is_current=True,
        engagement_name_snapshot="Deleted Engagement Snapshot",
    )
    _add_report(
        db,
        tenant_id=tenant.id,
        user_id=other_user.id,
        engagement_id=other_engagement.id,
        version=1,
        is_current=True,
        engagement_name_snapshot="Foreign User Snapshot",
    )
    db.execute(delete(Engagement).where(Engagement.id == engagement.id))
    db.commit()

    service = ReportReadService(db)
    library = service.list_report_library(
        tenant_id=tenant.id,
        user_id=user.id,
        query="deleted engagement",
    )
    direct = service.get_report(
        tenant_id=tenant.id,
        user_id=user.id,
        report_id=report.id,
    )

    assert library.total == 1
    assert library.reports[0].report_id == report.id
    assert library.reports[0].engagement_id == report.engagement_id
    assert library.reports[0].engagement_name_snapshot == "Deleted Engagement Snapshot"
    assert library.reports[0].source_task_count == 1
    assert library.reports[0].source_knowledge_count == 1
    assert library.reports[0].source_evidence_count == 1
    assert direct is not None
    assert direct.id == report.id
    assert direct.markdown_snapshot == "# Report 1"


def test_direct_report_read_returns_full_sections_when_scope_matches() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="direct-owner")
    _, other_user, _ = _seed_scope(db, label="direct-other")
    report = _add_report(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        is_current=True,
    )
    report.sections = [_valid_report_section()]
    db.commit()

    service = ReportReadService(db)
    direct = service.get_report(
        tenant_id=tenant.id,
        user_id=user.id,
        report_id=report.id,
    )
    missing = service.get_report(
        tenant_id=tenant.id,
        user_id=user.id,
        report_id=uuid.uuid4(),
    )
    cross_user = service.get_report(
        tenant_id=tenant.id,
        user_id=other_user.id,
        report_id=report.id,
    )

    assert direct is not None
    assert direct.id == report.id
    assert direct.sections[0].content_markdown == "Full section content"
    assert direct.markdown_snapshot == "# Report 1"
    assert missing is None
    assert cross_user is None


def test_job_status_returns_shape_or_none_for_router_404_handling() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="job-owner")
    _, other_user, _ = _seed_scope(db, label="job-other")
    report = _add_report(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        is_current=True,
    )
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        report_id=report.id,
        status="generating",
    )
    db.commit()

    service = ReportReadService(db)
    status = service.get_job_status(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        job_id=job.id,
    )
    missing = service.get_job_status(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        job_id=uuid.uuid4(),
    )
    cross_user = service.get_job_status(
        tenant_id=tenant.id,
        user_id=other_user.id,
        requested_by_user_id=user.id,
        engagement_id=engagement.id,
        job_id=job.id,
    )
    cross_requester = service.get_job_status(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=other_user.id,
        engagement_id=engagement.id,
        job_id=job.id,
    )

    assert status is not None
    assert status.id == job.id
    assert status.report_id == report.id
    assert status.report_type == "pentest"
    assert status.status == "generating"
    assert status.total_sections == 3
    assert status.attempt_count == 0
    assert status.max_attempts == 3
    assert missing is None
    assert cross_user is None
    assert cross_requester is None
    assert db.query(EngagementReportJob).count() == 1


def test_job_status_projects_safe_failure_details_from_linked_report() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="job-failure-details")
    report = _add_report(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        status="failed",
        generation_metadata={
            "failed_section_id": "vulnerability_summary",
            "failed_section_order": 4,
            "failed_section_type": "findings",
            "validation_issues": [
                {
                    "code": "transcript_only_reportable_content",
                    "path": "source_refs",
                    "message": "raw message should not project",
                }
            ],
            "raw_response": "SECRET_RAW_OUTPUT",
        },
    )
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        report_id=report.id,
        status="failed",
    )
    db.commit()

    status = ReportReadService(db).get_job_status_by_id(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        job_id=job.id,
    )

    assert status is not None
    assert status.failure_details is not None
    assert status.failure_details.failed_section_id == "vulnerability_summary"
    assert status.failure_details.failed_section_order == 4
    assert status.failure_details.failed_section_type == "findings"
    assert [issue.model_dump() for issue in status.failure_details.validation_issues] == [
        {"code": "transcript_only_reportable_content", "path": "source_refs"}
    ]
    assert "SECRET_RAW_OUTPUT" not in status.model_dump_json()


def test_job_status_by_id_requires_requester_scope_and_owned_engagement() -> None:
    db = _build_session()
    tenant, user, engagement = _seed_scope(db, label="job-id-owner")
    _, other_user, _ = _seed_scope(db, label="job-id-other")
    job = _add_job(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        status="queued",
    )
    db.commit()

    service = ReportReadService(db)
    status = service.get_job_status_by_id(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        job_id=job.id,
    )
    cross_requester = service.get_job_status_by_id(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=other_user.id,
        job_id=job.id,
    )
    missing = service.get_job_status_by_id(
        tenant_id=tenant.id,
        user_id=user.id,
        requested_by_user_id=user.id,
        job_id=uuid.uuid4(),
    )

    assert status is not None
    assert status.id == job.id
    assert status.engagement_id == engagement.id
    assert status.current_section_id == "summary"
    assert status.completed_sections == ["intro"]
    assert status.total_sections == 3
    assert status.attempt_count == 0
    assert status.max_attempts == 3
    assert cross_requester is None
    assert missing is None
