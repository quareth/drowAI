"""Test canonical engagement-report artifact and lifecycle persistence."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.reporting import EngagementReport
from backend.models.tenant import Tenant
from backend.repositories.reporting.engagement_report_repository import (
    EngagementReportRepository,
)
from backend.services.reporting.contracts import (
    ENGAGEMENT_REPORT_SCHEMA_VERSION,
    GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY,
)


REPORTING_REPOSITORY_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    Task.__table__,
    EngagementReport.__table__,
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


def _add_report(
    session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    report_type: str,
    version: int,
    status: str,
    is_current: bool,
    source_task_memo_ids: list[str] | None = None,
    generation_metadata: dict | None = None,
) -> EngagementReport:
    report = EngagementReport(
        tenant_id=tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        report_type=report_type,
        version=version,
        status=status,
        is_current=is_current,
        title=f"Report {version}",
        sections=[{"id": "summary"}],
        source_task_memo_ids=list(source_task_memo_ids or []),
        source_knowledge_refs=[],
        source_evidence_refs=[],
        generation_metadata=dict(generation_metadata or {}),
    )
    session.add(report)
    session.flush()
    return report


def test_report_history_and_current_report_reads_are_scoped() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="report-owner")
        other_tenant, other_user, other_engagement, _ = _seed_scope(
            session, tenant_label="report-other"
        )

        old_report = _add_report(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=1,
            status="ready",
            is_current=False,
        )
        current_report = _add_report(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=2,
            status="ready",
            is_current=True,
        )
        _add_report(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="vulnerability_assessment",
            version=1,
            status="ready",
            is_current=True,
        )
        _add_report(
            session,
            tenant_id=other_tenant.id,
            user_id=other_user.id,
            engagement_id=other_engagement.id,
            report_type="pentest",
            version=1,
            status="ready",
            is_current=True,
        )

        repo = EngagementReportRepository(session)

        current = repo.get_current_ready_report(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
        )
        history = repo.list_report_history(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
        )

        assert current is not None
        assert current.id == current_report.id
        assert [report.id for report in history] == [current_report.id, old_report.id]

    engine.dispose()


def test_report_attempt_ready_promotion_and_failed_attempt_preserve_current() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="report-write")
        other_tenant, other_user, other_engagement, _ = _seed_scope(
            session, tenant_label="report-write-other"
        )
        memo_one_id = uuid.uuid4()
        memo_two_id = uuid.uuid4()
        current_report = _add_report(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=1,
            status="ready",
            is_current=True,
            source_task_memo_ids=[str(memo_one_id)],
            generation_metadata={
                GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY: "old-hash"
            },
        )
        other_type_current = _add_report(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="vulnerability_assessment",
            version=1,
            status="ready",
            is_current=True,
        )
        other_scope_current = _add_report(
            session,
            tenant_id=other_tenant.id,
            user_id=other_user.id,
            engagement_id=other_engagement.id,
            report_type="pentest",
            version=1,
            status="ready",
            is_current=True,
        )

        repo = EngagementReportRepository(session)
        next_version = repo.next_report_version(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
        )
        attempt = repo.create_report_attempt(
            tenant_id=tenant.id,
            user_id=user.id,
            created_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=next_version,
            title="Generated report",
            source_task_memo_ids=[memo_two_id, memo_one_id, memo_two_id],
            generation_metadata={"status": "started"},
        )
        assert attempt.status == "generating"
        assert attempt.is_current is False

        updated = repo.update_report_sections(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_id=attempt.id,
            sections=[{"id": "summary", "status": "ready"}],
        )
        ready = repo.mark_report_ready(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_id=attempt.id,
            markdown_snapshot="# Generated report",
            source_task_memo_ids=[memo_two_id, memo_one_id],
            source_knowledge_refs=[{"id": "knowledge-1"}],
            source_evidence_refs=[{"id": "evidence-1"}],
            generation_metadata={
                GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY: "new-hash"
            },
            generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        assert next_version == 2
        assert attempt.schema_version == ENGAGEMENT_REPORT_SCHEMA_VERSION
        assert attempt.source_task_memo_ids == sorted(
            [str(memo_one_id), str(memo_two_id)]
        )
        assert updated is not None
        assert updated.sections == [{"id": "summary", "status": "ready"}]
        assert ready is not None
        assert ready.status == "ready"
        assert ready.is_current is True
        assert ready.markdown_snapshot == "# Generated report"
        assert ready.source_knowledge_refs == [{"id": "knowledge-1"}]
        assert ready.source_evidence_refs == [{"id": "evidence-1"}]
        assert (
            ready.generation_metadata[GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY]
            == "new-hash"
        )
        assert ready.generated_at == datetime(2026, 1, 1)
        assert current_report.is_current is False
        assert other_type_current.is_current is True
        assert other_scope_current.is_current is True
        assert (
            repo.get_current_ready_report(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                report_type="pentest",
            ).id
            == ready.id
        )
        assert (
            repo.next_report_version(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                report_type="pentest",
            )
            == 3
        )
        assert (
            repo.next_report_version(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                report_type="vulnerability_assessment",
            )
            == 2
        )
        assert (
            repo.get_report_by_id(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                report_id=ready.id,
            ).id
            == ready.id
        )
        assert (
            repo.get_report_by_id(
                tenant_id=tenant.id,
                user_id=other_user.id,
                engagement_id=engagement.id,
                report_id=ready.id,
            )
            is None
        )
        assert (
            repo.get_report_by_id(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                report_id="not-a-uuid",
            )
            is None
        )

        failed_attempt = repo.create_report_attempt(
            tenant_id=tenant.id,
            user_id=user.id,
            created_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=3,
            title="Failed report",
            source_task_memo_ids=[memo_one_id],
        )
        failed = repo.mark_report_failed(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_id=failed_attempt.id,
            error_message="section generation failed",
            generation_metadata={"reason": "section_generation_failed"},
        )

        assert failed is not None
        assert failed.status == "failed"
        assert failed.is_current is False
        assert failed.error_message == "section generation failed"
        assert failed.generation_metadata == {"reason": "section_generation_failed"}
        assert ready.is_current is True
        assert (
            repo.get_current_ready_report(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                report_type="pentest",
            ).id
            == ready.id
        )

    engine.dispose()


def test_report_ready_promotion_defaults_generated_at_when_omitted() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(
            session, tenant_label="report-generated-at"
        )

        repo = EngagementReportRepository(session)
        attempt = repo.create_report_attempt(
            tenant_id=tenant.id,
            user_id=user.id,
            created_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=1,
            title="Generated at default report",
            source_task_memo_ids=[uuid.uuid4()],
        )

        ready = repo.mark_report_ready(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_id=attempt.id,
            markdown_snapshot="# Generated at default report",
            source_task_memo_ids=attempt.source_task_memo_ids,
            source_knowledge_refs=[],
            source_evidence_refs=[],
            generation_metadata={},
        )

        assert ready is not None
        assert ready.status == "ready"
        assert ready.is_current is True
        assert ready.generated_at is not None

    engine.dispose()


def test_ready_current_report_source_lookup_requires_current_report_and_hash() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="report-source")
        memo_one_id = uuid.uuid4()
        memo_two_id = uuid.uuid4()
        old_report = _add_report(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=1,
            status="ready",
            is_current=False,
            source_task_memo_ids=[str(memo_one_id), str(memo_two_id)],
            generation_metadata={
                GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY: "source-hash"
            },
        )
        current_report = _add_report(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=2,
            status="ready",
            is_current=True,
            source_task_memo_ids=[str(memo_two_id), str(memo_one_id)],
            generation_metadata={
                GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY: "source-hash"
            },
        )
        historical_only = _add_report(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="vulnerability_assessment",
            version=1,
            status="ready",
            is_current=False,
            source_task_memo_ids=[str(memo_one_id)],
            generation_metadata={
                GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY: "historical-hash"
            },
        )

        repo = EngagementReportRepository(session)

        matched = repo.find_ready_current_report_by_source(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            selected_task_memo_ids=[str(memo_one_id), str(memo_two_id)],
            source_watermark_hash="source-hash",
        )

        assert matched is not None
        assert matched.id == current_report.id
        assert matched.id != old_report.id
        assert (
            repo.find_ready_current_report_by_source(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                report_type="pentest",
                selected_task_memo_ids=[str(memo_one_id), str(memo_two_id)],
                source_watermark_hash="other-hash",
            )
            is None
        )
        assert (
            repo.find_ready_current_report_by_source(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                report_type="vulnerability_assessment",
                selected_task_memo_ids=historical_only.source_task_memo_ids,
                source_watermark_hash="historical-hash",
            )
            is None
        )

    engine.dispose()


def test_ready_report_source_lookup_distinguishes_deployment_identity() -> None:
    """V2 report reuse never collapses two deployments into an empty model key."""

    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(
            session,
            tenant_label="report-deployment-identity",
        )
        memo_id = uuid.uuid4()
        stored_selection = {
            "schema_version": 2,
            "deployment_ref": {
                "deployment_id": "11111111-1111-4111-8111-111111111111",
                "expected_revision": 2,
            },
            "reasoning_effort": "medium",
        }
        report = _add_report(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=1,
            status="ready",
            is_current=True,
            source_task_memo_ids=[str(memo_id)],
            generation_metadata={
                GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY: "source-hash",
                "llm_runtime_selection": stored_selection,
            },
        )
        repo = EngagementReportRepository(session)

        matched = repo.find_ready_current_report_by_source(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            selected_task_memo_ids=[str(memo_id)],
            source_watermark_hash="source-hash",
            llm_runtime_selection=stored_selection,
        )
        mismatched = repo.find_ready_current_report_by_source(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            selected_task_memo_ids=[str(memo_id)],
            source_watermark_hash="source-hash",
            llm_runtime_selection={
                **stored_selection,
                "deployment_ref": {
                    "deployment_id": "22222222-2222-4222-8222-222222222222",
                    "expected_revision": 2,
                },
            },
        )

        assert matched is not None
        assert matched.id == report.id
        assert mismatched is None

    engine.dispose()


def test_report_repository_methods_do_not_commit_transactions() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(
            session, tenant_label="no-commit-report"
        )
        report_source_id = uuid.uuid4()
        _add_report(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=1,
            status="ready",
            is_current=True,
            source_task_memo_ids=[str(report_source_id)],
            generation_metadata={
                GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY: "no-commit-hash"
            },
        )
        repo = EngagementReportRepository(session)
        session.commit = Mock(
            side_effect=AssertionError("repository methods must not commit")
        )

        repo.find_ready_current_report_by_source(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            selected_task_memo_ids=[report_source_id],
            source_watermark_hash="no-commit-hash",
        )
        next_report_version = repo.next_report_version(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
        )
        report_attempt = repo.create_report_attempt(
            tenant_id=tenant.id,
            user_id=user.id,
            created_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=next_report_version,
            title="No commit report",
            source_task_memo_ids=[report_source_id],
        )
        repo.update_report_sections(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_id=report_attempt.id,
            sections=[{"id": "summary"}],
        )
        repo.mark_report_ready(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_id=report_attempt.id,
            markdown_snapshot="# No commit report",
            source_task_memo_ids=[report_source_id],
            source_knowledge_refs=[],
            source_evidence_refs=[],
            generation_metadata={
                GENERATION_METADATA_SOURCE_WATERMARK_HASH_KEY: "no-commit-hash"
            },
        )
        failed_report_attempt = repo.create_report_attempt(
            tenant_id=tenant.id,
            user_id=user.id,
            created_by_user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=next_report_version + 1,
            title="No commit failed report",
            source_task_memo_ids=[report_source_id],
        )
        repo.mark_report_failed(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_id=failed_report_attempt.id,
            error_message="failed",
        )
        repo.clear_current_ready_reports_for_type(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="vulnerability_assessment",
        )
        repo.get_report_by_id(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_id=report_attempt.id,
        )

        session.commit.assert_not_called()

    engine.dispose()


def test_report_deletion_lifecycle_preserves_content_then_finalizes_tombstone() -> None:
    engine, factory = _make_session_factory()
    with factory() as session:
        tenant, user, engagement, _ = _seed_scope(session, tenant_label="deletion")
        source_memo_id = uuid.uuid4()
        report = _add_report(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            version=1,
            status="ready",
            is_current=True,
            source_task_memo_ids=[str(source_memo_id)],
            generation_metadata={"trace": "kept-until-finalized"},
        )
        report.markdown_snapshot = "# Report"
        session.flush()
        scheduled_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        undo_until = datetime(2026, 1, 2, tzinfo=timezone.utc)
        finalized_at = datetime(2026, 1, 3, tzinfo=timezone.utc)
        repo = EngagementReportRepository(session)

        scheduled = repo.schedule_report_deletion(
            report=report,
            deleted_by_user_id=user.id,
            reason="requested",
            scheduled_at=scheduled_at,
            undo_until=undo_until,
            metadata={"request": "manual"},
        )

        assert scheduled.is_current is False
        assert scheduled.markdown_snapshot == "# Report"
        assert scheduled.source_task_memo_ids == [str(source_memo_id)]
        assert (
            repo.get_report_by_id_for_lifecycle(
                tenant_id=tenant.id, user_id=user.id, report_id=report.id
            )
            is not None
        )
        assert (
            repo.get_report_by_id_for_owned_engagement(
                tenant_id=tenant.id, user_id=user.id, report_id=report.id
            )
            is None
        )

        cancelled = repo.cancel_report_deletion(report=report)
        assert cancelled.delete_scheduled_at is None
        assert cancelled.deletion_metadata is None

        repo.schedule_report_deletion(
            report=report,
            deleted_by_user_id=user.id,
            reason="requested",
            scheduled_at=scheduled_at,
            undo_until=undo_until,
        )
        finalized = repo.finalize_report_deletion(
            report=report, finalized_at=finalized_at
        )

        assert finalized.sections == []
        assert finalized.markdown_snapshot is None
        assert finalized.source_task_memo_ids == []
        assert finalized.generation_metadata == {}
        assert finalized.deletion_finalized_at == finalized_at.replace(tzinfo=None)
        assert finalized.deletion_metadata["content_erased"] is True
        assert (
            repo.get_report_by_id_for_lifecycle(
                tenant_id=tenant.id, user_id=user.id, report_id=report.id
            )
            is None
        )

    engine.dispose()
