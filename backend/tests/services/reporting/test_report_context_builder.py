"""Tests for selected-memo engagement report context construction."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend import models as backend_models
from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import (
    KnowledgeEntityProvenance,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
)
from backend.models.reporting import TaskClosureMemo
from backend.models.tenant import Tenant
from backend.services.reporting.report_evidence_timeline import (
    build_report_evidence_timeline,
)
from backend.services.reporting.report_context_builder import ReportContextBuilder


def _build_session() -> tuple[Session, object]:
    assert backend_models.__all__
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    return factory(), engine


def _seed_scope(db: Session, *, label: str) -> tuple[Tenant, User, Engagement]:
    tenant = Tenant(
        slug=f"tenant-{label}-{uuid.uuid4().hex[:8]}",
        name=f"Tenant {label}",
    )
    user = User(username=f"user-{label}-{uuid.uuid4().hex[:8]}", password="hashed")
    db.add_all([tenant, user])
    db.flush()

    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name=f"Engagement {label}",
        description="Engagement description",
    )
    db.add(engagement)
    db.flush()
    return tenant, user, engagement


def _add_task(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    name: str,
    status: str = TaskStatus.STOPPED.value,
) -> Task:
    task = Task(
        tenant_id=tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        name=name,
        description=f"{name} description",
        scope=f"{name} scope",
        status=status,
        stopped_at=datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc),
    )
    db.add(task)
    db.flush()
    return task


def _memo_body(
    *,
    evidence_ref: str,
    knowledge_ref: str,
    summary: str = "memo summary",
) -> dict:
    return {
        "summary": summary,
        "actions_performed": [{"text": "Ran service enumeration.", "source": "tool"}],
        "reportable_observations": [
            {
                "text": "HTTPS was exposed.",
                "evidence_refs": [evidence_ref],
                "knowledge_refs": [knowledge_ref],
            }
        ],
        "possible_findings": [
            {
                "title": "Outdated service",
                "description": "The service banner indicates an old version.",
                "severity": "medium",
                "confidence": "high",
                "evidence_refs": [evidence_ref],
                "knowledge_refs": [knowledge_ref],
            }
        ],
        "limitations": [{"text": "Credentialed checks were not performed."}],
        "unsupported_notes": [{"text": "One transcript claim lacked durable evidence."}],
        "evidence_refs": [evidence_ref],
        "knowledge_refs": [knowledge_ref],
    }


def _add_memo(
    db: Session,
    *,
    task: Task,
    user_id: int,
    engagement_id: int,
    memo_mode: str,
    memo: dict,
    version: int = 1,
) -> TaskClosureMemo:
    row = TaskClosureMemo(
        tenant_id=task.tenant_id,
        user_id=user_id,
        created_by_user_id=user_id,
        engagement_id=engagement_id,
        task_id=task.id,
        version=version,
        is_current=True,
        status="ready",
        memo_mode=memo_mode,
        source_watermark={"schema_version": 1, "task_id": task.id, "version": version},
        memo=memo,
        generated_at=datetime(2026, 6, 9, 13, 0, tzinfo=timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


def _add_evidence(
    db: Session,
    *,
    task: Task,
    user_id: int,
    engagement_id: int,
    excerpt: str,
) -> KnowledgeEvidenceArchive:
    row = KnowledgeEvidenceArchive(
        tenant_id=task.tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        task_id=task.id,
        source_execution_id=uuid.uuid4(),
        storage_mode="inline_excerpt",
        inline_excerpt=excerpt,
        lineage_snapshot={"target": f"task-{task.id}:443", "source_tool": "nmap"},
        archive_metadata={"type": "service"},
        created_at=datetime(2026, 6, 9, 12, 30, tzinfo=timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


def _add_finding_with_provenance(
    db: Session,
    *,
    task: Task,
    user_id: int,
    engagement_id: int,
    title: str,
    evidence_id,
    candidate: bool = False,
) -> KnowledgeFinding:
    now = datetime(2026, 6, 9, 12, 20, tzinfo=timezone.utc)
    finding = KnowledgeFinding(
        tenant_id=task.tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        finding_key=f"finding-{task.id}-{uuid.uuid4().hex[:8]}",
        finding_type="service",
        subject_type="service",
        subject_key=f"task-{task.id}:443",
        title=title,
        severity="medium",
        status="candidate" if candidate else "open",
        assertion_level="candidate" if candidate else "confirmed",
        confidence="medium",
        first_seen_at=now,
        last_seen_at=now,
        evidence_summary={"evidence_refs": [{"evidence_archive_id": str(evidence_id)}]},
        finding_metadata={},
    )
    db.add(finding)
    db.flush()

    provenance = KnowledgeEntityProvenance(
        tenant_id=task.tenant_id,
        user_id=user_id,
        entity_type="finding",
        entity_id=finding.id,
        engagement_id=engagement_id,
        task_id=task.id,
        execution_id=uuid.uuid4(),
        tool_name="nmap",
        observed_at=now,
        confidence="medium",
        evidence_archive_id=evidence_id,
    )
    db.add(provenance)
    db.flush()
    return finding


def test_context_uses_selected_memos_and_excludes_unrelated_sources() -> None:
    db, engine = _build_session()
    try:
        tenant, user, engagement = _seed_scope(db, label="context")
        selected_task = _add_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Selected",
        )
        unrelated_task = _add_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Unrelated",
        )
        selected_evidence = _add_evidence(
            db,
            task=selected_task,
            user_id=user.id,
            engagement_id=engagement.id,
            excerpt="Selected task evidence",
        )
        unrelated_evidence = _add_evidence(
            db,
            task=unrelated_task,
            user_id=user.id,
            engagement_id=engagement.id,
            excerpt="Unrelated task evidence",
        )
        selected_finding = _add_finding_with_provenance(
            db,
            task=selected_task,
            user_id=user.id,
            engagement_id=engagement.id,
            title="Selected finding",
            evidence_id=selected_evidence.id,
        )
        unrelated_finding = _add_finding_with_provenance(
            db,
            task=unrelated_task,
            user_id=user.id,
            engagement_id=engagement.id,
            title="Unrelated finding",
            evidence_id=unrelated_evidence.id,
        )
        memo = _add_memo(
            db,
            task=selected_task,
            user_id=user.id,
            engagement_id=engagement.id,
            memo_mode="supported",
            memo=_memo_body(
                evidence_ref=f"evidence_archive:{selected_evidence.id}",
                knowledge_ref=f"knowledge_finding:{selected_finding.id}",
            ),
        )
        db.commit()

        context = ReportContextBuilder(db).build(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            selected_memos=[memo],
        )

        assert context.allowed_task_memo_ids == {str(memo.id)}
        assert [memo_context.memo_id for memo_context in context.selected_memos] == [
            str(memo.id)
        ]
        assert context.memo_partitions.supported_memo_ids == (str(memo.id),)
        assert context.selected_memos[0].body.reportable_observations
        assert context.selected_memos[0].body.possible_findings
        assert context.allowed_evidence_refs == {f"evidence_archive:{selected_evidence.id}"}
        assert context.allowed_knowledge_refs == {f"knowledge_finding:{selected_finding.id}"}
        assert f"evidence_archive:{unrelated_evidence.id}" not in context.allowed_evidence_refs
        assert f"knowledge_finding:{unrelated_finding.id}" not in context.allowed_knowledge_refs
        timeline = build_report_evidence_timeline(context)
        assert [item.ref for item in timeline] == [
            f"evidence_archive:{selected_evidence.id}"
        ]
        assert timeline[0].source_tool == "Nmap"
        assert timeline[0].target == f"task-{selected_task.id}:443"
        assert "Selected task evidence" in timeline[0].summary
    finally:
        db.close()
        engine.dispose()


def test_limited_memos_expose_only_limitations_and_unsupported_notes() -> None:
    db, engine = _build_session()
    try:
        tenant, user, engagement = _seed_scope(db, label="limited")
        task = _add_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Limited",
        )
        memo = _add_memo(
            db,
            task=task,
            user_id=user.id,
            engagement_id=engagement.id,
            memo_mode="limited",
            memo=_memo_body(
                evidence_ref="evidence_archive:should-not-ground",
                knowledge_ref="knowledge_finding:should-not-ground",
            ),
        )
        db.commit()

        context = ReportContextBuilder(db).build(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="vulnerability_assessment",
            selected_memos=[memo],
        )

        body = context.selected_memos[0].body
        assert context.memo_partitions.limited_memo_ids == (str(memo.id),)
        assert body.actions_performed == ()
        assert body.limitations
        assert body.unsupported_notes
        assert body.reportable_observations == ()
        assert body.possible_findings == ()
        assert body.evidence_refs == ()
        assert body.knowledge_refs == ()
    finally:
        db.close()
        engine.dispose()


def test_mixed_supported_and_limited_memos_keep_limited_content_restricted() -> None:
    db, engine = _build_session()
    try:
        tenant, user, engagement = _seed_scope(db, label="mixed-mode")
        supported_task = _add_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Supported",
        )
        limited_task = _add_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Limited",
        )
        supported_evidence = _add_evidence(
            db,
            task=supported_task,
            user_id=user.id,
            engagement_id=engagement.id,
            excerpt="Supported task evidence",
        )
        limited_evidence = _add_evidence(
            db,
            task=limited_task,
            user_id=user.id,
            engagement_id=engagement.id,
            excerpt="Limited task evidence",
        )
        supported_finding = _add_finding_with_provenance(
            db,
            task=supported_task,
            user_id=user.id,
            engagement_id=engagement.id,
            title="Supported finding",
            evidence_id=supported_evidence.id,
        )
        limited_finding = _add_finding_with_provenance(
            db,
            task=limited_task,
            user_id=user.id,
            engagement_id=engagement.id,
            title="Limited finding",
            evidence_id=limited_evidence.id,
        )
        supported_memo = _add_memo(
            db,
            task=supported_task,
            user_id=user.id,
            engagement_id=engagement.id,
            memo_mode="supported",
            memo=_memo_body(
                evidence_ref=f"evidence_archive:{supported_evidence.id}",
                knowledge_ref=f"knowledge_finding:{supported_finding.id}",
            ),
        )
        limited_memo = _add_memo(
            db,
            task=limited_task,
            user_id=user.id,
            engagement_id=engagement.id,
            memo_mode="limited",
            memo=_memo_body(
                evidence_ref=f"evidence_archive:{limited_evidence.id}",
                knowledge_ref=f"knowledge_finding:{limited_finding.id}",
            ),
        )
        db.commit()

        context = ReportContextBuilder(db).build(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            selected_memos=[limited_memo, supported_memo],
        )

        assert context.memo_partitions.supported_memo_ids == (str(supported_memo.id),)
        assert context.memo_partitions.limited_memo_ids == (str(limited_memo.id),)
        assert context.allowed_evidence_refs == {
            f"evidence_archive:{supported_evidence.id}"
        }
        assert context.allowed_knowledge_refs == {
            f"knowledge_finding:{supported_finding.id}"
        }
        assert (
            f"evidence_archive:{limited_evidence.id}" not in context.allowed_evidence_refs
        )
        assert (
            f"knowledge_finding:{limited_finding.id}"
            not in context.allowed_knowledge_refs
        )
        limited_body = next(
            memo.body
            for memo in context.selected_memos
            if memo.memo_id == str(limited_memo.id)
        )
        assert limited_body.actions_performed == ()
        assert limited_body.limitations
        assert limited_body.unsupported_notes
        assert limited_body.reportable_observations == ()
        assert limited_body.possible_findings == ()
        assert limited_body.evidence_refs == ()
        assert limited_body.knowledge_refs == ()
    finally:
        db.close()
        engine.dispose()


def test_context_rejects_selected_memo_when_task_is_not_stopped() -> None:
    db, engine = _build_session()
    try:
        tenant, user, engagement = _seed_scope(db, label="not-stopped")
        task = _add_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Running",
            status=TaskStatus.RUNNING.value,
        )
        memo = _add_memo(
            db,
            task=task,
            user_id=user.id,
            engagement_id=engagement.id,
            memo_mode="supported",
            memo=_memo_body(
                evidence_ref="evidence_archive:running",
                knowledge_ref="knowledge_finding:running",
            ),
        )
        db.commit()

        with pytest.raises(ValueError, match="selected memo task must belong"):
            ReportContextBuilder(db).build(
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                report_type="pentest",
                selected_memos=[memo],
            )
    finally:
        db.close()
        engine.dispose()


def test_candidate_policy_filters_candidate_only_knowledge_refs() -> None:
    db, engine = _build_session()
    try:
        tenant, user, engagement = _seed_scope(db, label="candidate")
        task = _add_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Candidate",
        )
        evidence = _add_evidence(
            db,
            task=task,
            user_id=user.id,
            engagement_id=engagement.id,
            excerpt="Candidate evidence",
        )
        candidate_finding = _add_finding_with_provenance(
            db,
            task=task,
            user_id=user.id,
            engagement_id=engagement.id,
            title="Candidate finding",
            evidence_id=evidence.id,
            candidate=True,
        )
        memo = _add_memo(
            db,
            task=task,
            user_id=user.id,
            engagement_id=engagement.id,
            memo_mode="supported",
            memo=_memo_body(
                evidence_ref=f"evidence_archive:{evidence.id}",
                knowledge_ref=f"knowledge_finding:{candidate_finding.id}",
            ),
        )
        db.commit()

        default_context = ReportContextBuilder(db).build(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            selected_memos=[memo],
        )
        included_context = ReportContextBuilder(db).build(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            selected_memos=[memo],
            include_candidate_findings=True,
        )

        candidate_ref = f"knowledge_finding:{candidate_finding.id}"
        evidence_ref = f"evidence_archive:{evidence.id}"
        assert candidate_ref not in default_context.allowed_knowledge_refs
        assert evidence_ref not in default_context.allowed_evidence_refs
        assert candidate_ref in included_context.allowed_knowledge_refs
        assert evidence_ref in included_context.allowed_evidence_refs
        assert included_context.candidate_policy.include_candidate_findings is True
    finally:
        db.close()
        engine.dispose()


def test_context_is_deterministic_and_bounded_for_prompt_rendering() -> None:
    db, engine = _build_session()
    try:
        tenant, user, engagement = _seed_scope(db, label="deterministic")
        first_task = _add_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="A",
        )
        second_task = _add_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="B",
        )
        first_memo = _add_memo(
            db,
            task=first_task,
            user_id=user.id,
            engagement_id=engagement.id,
            memo_mode="supported",
            memo=_memo_body(
                evidence_ref="evidence_archive:first",
                knowledge_ref="knowledge_finding:first",
                summary="first",
            ),
        )
        second_memo = _add_memo(
            db,
            task=second_task,
            user_id=user.id,
            engagement_id=engagement.id,
            memo_mode="supported",
            memo={
                **_memo_body(
                    evidence_ref="evidence_archive:second",
                    knowledge_ref="knowledge_finding:second",
                    summary="second",
                ),
                "reportable_observations": [
                    {"text": "one " * 40, "evidence_refs": ["evidence_archive:second"]},
                    {"text": "two", "evidence_refs": ["evidence_archive:second"]},
                ],
            },
        )
        db.commit()

        context = ReportContextBuilder(
            db,
            max_memo_items=1,
            max_memo_text_characters=30,
        ).build(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            selected_memos=[second_memo, first_memo],
        )

        assert [memo.task_id for memo in context.selected_memos] == [
            first_task.id,
            second_task.id,
        ]
        assert len(context.selected_memos[1].body.reportable_observations) == 1
        assert context.selected_memos[1].body.reportable_observations[0]["text"].endswith(
            "..."
        )
        assert [task.task_id for task in context.selected_tasks] == [
            first_task.id,
            second_task.id,
        ]
        assert [
            watermark.memo_id for watermark in context.source_watermark.selected_memos
        ] == [str(first_memo.id), str(second_memo.id)]
        assert context.source_watermark.hash == context.source_watermark.job_source_watermark[
            "hash"
        ]
        expected_persisted_memos = [
            {
                "memo_id": str(first_memo.id),
                "version": first_memo.version,
                "source_watermark": {
                    "schema_version": 1,
                    "task_id": first_task.id,
                    "version": first_memo.version,
                },
            },
            {
                "memo_id": str(second_memo.id),
                "version": second_memo.version,
                "source_watermark": {
                    "schema_version": 1,
                    "task_id": second_task.id,
                    "version": second_memo.version,
                },
            },
        ]
        assert context.source_watermark.job_source_watermark["selected_memos"] == sorted(
            expected_persisted_memos,
            key=lambda item: item["memo_id"],
        )
        assert context.source_watermark.generation_metadata["source_watermark_hash"] == (
            context.source_watermark.hash
        )
    finally:
        db.close()
        engine.dispose()


def test_context_watermarks_all_request_valid_selected_memos_above_legacy_bound() -> None:
    db, engine = _build_session()
    try:
        tenant, user, engagement = _seed_scope(db, label="watermark-bound")
        memos: list[TaskClosureMemo] = []
        for index in range(51):
            task = _add_task(
                db,
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                name=f"Selected {index}",
            )
            memos.append(
                _add_memo(
                    db,
                    task=task,
                    user_id=user.id,
                    engagement_id=engagement.id,
                    memo_mode="supported",
                    memo=_memo_body(
                        evidence_ref=f"evidence_archive:{index}",
                        knowledge_ref=f"knowledge_finding:{index}",
                        summary=f"memo {index}",
                    ),
                )
            )
        db.commit()

        context = ReportContextBuilder(db).build(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            report_type="pentest",
            selected_memos=list(reversed(memos)),
        )

        expected_memo_ids = {str(memo.id) for memo in memos}
        watermark_memo_ids = {
            watermark.memo_id for watermark in context.source_watermark.selected_memos
        }
        persisted_memo_ids = {
            item["memo_id"]
            for item in context.source_watermark.job_source_watermark["selected_memos"]
        }
        assert len(context.selected_memos) == 51
        assert context.allowed_task_memo_ids == expected_memo_ids
        assert watermark_memo_ids == expected_memo_ids
        assert persisted_memo_ids == expected_memo_ids
        assert context.truncated is False
    finally:
        db.close()
        engine.dispose()
