"""Tests for task-local durable knowledge packet construction."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import (
    KnowledgeAsset,
    KnowledgeEntityProvenance,
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeIngestionRun,
    KnowledgeObservation,
    KnowledgeService,
)
from backend.models.tenant import Tenant
from backend.services.reporting.knowledge_packet_builder import KnowledgePacketBuilder


KNOWLEDGE_PACKET_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    Task.__table__,
    KnowledgeIngestionRun.__table__,
    KnowledgeObservation.__table__,
    KnowledgeEvidenceArchive.__table__,
    KnowledgeAsset.__table__,
    KnowledgeService.__table__,
    KnowledgeFinding.__table__,
    KnowledgeEntityProvenance.__table__,
]


def _make_session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine, tables=KNOWLEDGE_PACKET_TABLES)
    return engine, sessionmaker(
        bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
    )


def _seed_scope(session, *, label: str):
    tenant = Tenant(slug=f"tenant-{label}-{uuid.uuid4().hex}", name=f"Tenant {label}")
    user = User(username=f"user-{label}-{uuid.uuid4().hex}", password="hashed-password")
    session.add_all([tenant, user])
    session.flush()

    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name=f"Engagement {label}",
    )
    session.add(engagement)
    session.flush()

    task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name=f"Task {label}",
    )
    session.add(task)
    session.flush()
    return tenant, user, engagement, task


def _seed_run(
    session, *, tenant_id: int, user_id: int, engagement_id: int, task_id: int
):
    run = KnowledgeIngestionRun(
        tenant_id=tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        task_id=task_id,
        source_execution_id=uuid.uuid4(),
        extractor_family="memo.packet.test",
        extractor_version="1",
        status="completed",
    )
    session.add(run)
    session.flush()
    return run


def _seed_finding(
    session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    now: datetime,
    suffix: str,
    status: str = "open",
    assertion_level: str = "observed",
    confidence: str = "high",
    candidate_only: bool = False,
) -> KnowledgeFinding:
    asset = KnowledgeAsset(
        tenant_id=tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        asset_key=f"host.ip:10.0.0.{suffix}",
        asset_type="host.ip",
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(asset)
    session.flush()

    finding = KnowledgeFinding(
        tenant_id=tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        finding_key=f"finding.vulnerability:10.0.0.{suffix}:openssl",
        finding_type="finding.vulnerability",
        subject_type="host.ip",
        subject_key=asset.asset_key,
        asset_id=asset.id,
        title=f"OpenSSL issue {suffix}",
        severity="critical",
        status=status,
        assertion_level=assertion_level,
        confidence=confidence,
        first_seen_at=now,
        last_seen_at=now,
        evidence_summary={
            "evidence_refs": [{"evidence_archive_id": f"ev-summary-{suffix}"}]
        },
        finding_metadata={
            "source_tool": "nmap",
            "authority": {"candidate_only": candidate_only},
        },
    )
    session.add(finding)
    session.flush()
    return finding


def _add_observation(
    session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    task_id: int,
    run: KnowledgeIngestionRun,
    observed_at: datetime,
    suffix: str,
) -> KnowledgeObservation:
    observation = KnowledgeObservation(
        tenant_id=tenant_id,
        user_id=user_id,
        ingestion_run_id=run.id,
        engagement_id=engagement_id,
        task_id=task_id,
        source_execution_id=run.source_execution_id,
        observation_type="finding.vulnerability",
        subject_type="host.ip",
        subject_key=f"host.ip:10.0.0.{suffix}",
        assertion_level="observed",
        dedupe_key=f"dedupe-{suffix}-{uuid.uuid4().hex}",
        payload={
            "confidence": "high",
            "evidence_refs": [{"evidence_archive_id": f"ev-observation-{suffix}"}],
        },
        observed_at=observed_at,
    )
    session.add(observation)
    session.flush()
    return observation


def _add_provenance(
    session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    task_id: int,
    run: KnowledgeIngestionRun,
    finding: KnowledgeFinding,
    observed_at: datetime,
    evidence_archive_id: object | None = None,
) -> KnowledgeEntityProvenance:
    provenance = KnowledgeEntityProvenance(
        tenant_id=tenant_id,
        user_id=user_id,
        entity_type="finding",
        entity_id=finding.id,
        engagement_id=engagement_id,
        task_id=task_id,
        execution_id=run.source_execution_id,
        ingestion_run_id=run.id,
        observed_at=observed_at,
        confidence=finding.confidence,
        evidence_archive_id=evidence_archive_id,
    )
    session.add(provenance)
    session.flush()
    return provenance


def test_knowledge_packet_includes_task_local_canonical_finding_lineage() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="finding")
        now = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
        run = _seed_run(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        evidence = KnowledgeEvidenceArchive(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=run.source_execution_id,
            storage_mode="object_ref",
            object_key="knowledge/finding.json",
            content_sha256="a" * 64,
            lineage_snapshot={"task_id": task.id},
        )
        session.add(evidence)
        session.flush()
        finding = _seed_finding(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            now=now,
            suffix="10",
        )
        _add_observation(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            run=run,
            observed_at=now,
            suffix="10",
        )
        provenance = _add_provenance(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            run=run,
            finding=finding,
            observed_at=now + timedelta(seconds=1),
            evidence_archive_id=evidence.id,
        )

        packet = KnowledgePacketBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        finding_item = next(
            item
            for item in packet.items
            if item.ref == f"knowledge_finding:{finding.id}"
        )
        assert finding_item.record_id == str(finding.id)
        assert finding_item.record_type == "finding"
        assert finding_item.authoritative is True
        assert finding_item.authority == "task_local_canonical"
        assert finding_item.confidence == "high"
        assert finding_item.assertion_level == "observed"
        assert str(run.source_execution_id) in finding_item.source_execution_ids
        assert str(run.id) in finding_item.ingestion_run_ids
        assert str(evidence.id) in finding_item.evidence_archive_refs
        assert (
            f"knowledge_entity_provenance:{provenance.id}"
            in finding_item.provenance_refs
        )
        assert "OpenSSL issue 10" in finding_item.summary
        assert packet.canonical_item_count == 1
        assert packet.observation_item_count == 1
    finally:
        session.close()
        engine.dispose()


def test_knowledge_packet_is_deterministic_under_repeated_calls() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="deterministic")
        now = datetime(2026, 6, 9, 9, 30, tzinfo=timezone.utc)
        run = _seed_run(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        evidence = KnowledgeEvidenceArchive(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            source_execution_id=run.source_execution_id,
            storage_mode="object_ref",
            object_key="knowledge/deterministic.json",
            content_sha256="b" * 64,
            lineage_snapshot={"task_id": task.id},
        )
        session.add(evidence)
        session.flush()
        finding = _seed_finding(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            now=now,
            suffix="15",
        )
        _add_observation(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            run=run,
            observed_at=now,
            suffix="15",
        )
        _add_provenance(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            run=run,
            finding=finding,
            observed_at=now + timedelta(seconds=1),
            evidence_archive_id=evidence.id,
        )

        builder = KnowledgePacketBuilder(session)
        first_packet = builder.build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        second_packet = builder.build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        assert first_packet == second_packet
    finally:
        session.close()
        engine.dispose()


def test_knowledge_packet_marks_candidate_findings_low_authority() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="candidate")
        now = datetime(2026, 6, 9, 10, 0, tzinfo=timezone.utc)
        run = _seed_run(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        finding = _seed_finding(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            now=now,
            suffix="20",
            status="candidate",
            assertion_level="candidate",
            confidence="low",
            candidate_only=True,
        )
        _add_provenance(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            run=run,
            finding=finding,
            observed_at=now,
        )

        packet = KnowledgePacketBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        finding_item = next(
            item
            for item in packet.items
            if item.ref == f"knowledge_finding:{finding.id}"
        )
        assert finding_item.authoritative is False
        assert finding_item.authority == "candidate_low_authority"
        assert finding_item.assertion_level == "candidate"
        assert packet.candidate_item_count == 1
        assert all(
            item.authoritative is False
            for item in packet.items
            if item.record_type == "finding" and item.assertion_level == "candidate"
        )
    finally:
        session.close()
        engine.dispose()


def test_knowledge_packet_excludes_other_task_and_unproven_canonical_rows() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, selected_task = _seed_scope(session, label="scope")
        other_task = Task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            name="Other task",
        )
        session.add(other_task)
        session.flush()
        now = datetime(2026, 6, 9, 11, 0, tzinfo=timezone.utc)
        selected_run = _seed_run(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=selected_task.id,
        )
        other_run = _seed_run(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=other_task.id,
        )
        selected_finding = _seed_finding(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            now=now,
            suffix="30",
        )
        other_finding = _seed_finding(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            now=now,
            suffix="31",
        )
        unproven_finding = _seed_finding(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            now=now,
            suffix="32",
        )
        _add_provenance(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=selected_task.id,
            run=selected_run,
            finding=selected_finding,
            observed_at=now,
        )
        _add_provenance(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=other_task.id,
            run=other_run,
            finding=other_finding,
            observed_at=now,
        )
        _add_observation(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=other_task.id,
            run=other_run,
            observed_at=now,
            suffix="31",
        )

        packet = KnowledgePacketBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=selected_task.id,
        )
        refs = {item.ref for item in packet.items}

        assert f"knowledge_finding:{selected_finding.id}" in refs
        assert f"knowledge_finding:{other_finding.id}" not in refs
        assert f"knowledge_finding:{unproven_finding.id}" not in refs
        assert all("10.0.0.31" not in item.summary for item in packet.items)
    finally:
        session.close()
        engine.dispose()
