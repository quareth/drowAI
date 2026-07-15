"""Tests for task-local durable evidence packet construction."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import (
    KnowledgeEntityProvenance,
    KnowledgeEvidenceArchive,
)
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.runner_control import ExecutionSite, Runner, RuntimeJob
from backend.models.tenant import Tenant
from backend.services.reporting.evidence_packet_builder import EvidencePacketBuilder


EVIDENCE_PACKET_TABLES = [
    Tenant.__table__,
    User.__table__,
    Engagement.__table__,
    Task.__table__,
    ExecutionSite.__table__,
    Runner.__table__,
    RuntimeJob.__table__,
    ToolExecution.__table__,
    ExecutionArtifact.__table__,
    KnowledgeEvidenceArchive.__table__,
    KnowledgeEntityProvenance.__table__,
]


def _make_session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine, tables=EVIDENCE_PACKET_TABLES)
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


def _seed_execution(
    session,
    *,
    tenant_id: int,
    task_id: int,
    tool_name: str,
    started_at: datetime,
) -> ToolExecution:
    execution = ToolExecution(
        tenant_id=tenant_id,
        task_id=task_id,
        tool_name=tool_name,
        tool_arguments={},
        agent_path="langgraph",
        status="completed",
        started_at=started_at,
    )
    session.add(execution)
    session.flush()
    return execution


def _seed_artifact(
    session,
    *,
    execution: ToolExecution,
    artifact_kind: str,
    content_text: str | None,
    byte_size: int | None = None,
    mime_type: str | None = None,
) -> ExecutionArtifact:
    artifact = ExecutionArtifact(
        execution_id=execution.id,
        tenant_id=execution.tenant_id,
        task_id=execution.task_id,
        artifact_kind=artifact_kind,
        content_text=content_text,
        byte_size=byte_size,
        mime_type=mime_type,
    )
    session.add(artifact)
    session.flush()
    return artifact


def _seed_evidence(
    session,
    *,
    tenant_id: int,
    user_id: int,
    engagement_id: int,
    task_id: int,
    execution_id,
    artifact_id=None,
    inline_excerpt: str | None,
    created_at: datetime,
    lineage: dict | None = None,
    metadata: dict | None = None,
    byte_size: int | None = None,
    mime_type: str | None = None,
) -> KnowledgeEvidenceArchive:
    evidence = KnowledgeEvidenceArchive(
        tenant_id=tenant_id,
        user_id=user_id,
        engagement_id=engagement_id,
        task_id=task_id,
        source_execution_id=execution_id,
        source_artifact_id=artifact_id,
        storage_mode="inline_excerpt" if inline_excerpt else "metadata_only",
        inline_excerpt=inline_excerpt,
        byte_size=byte_size,
        mime_type=mime_type,
        lineage_snapshot=lineage or {},
        archive_metadata=metadata or {},
        created_at=created_at,
    )
    session.add(evidence)
    session.flush()
    return evidence


def test_evidence_packet_prefers_inline_excerpt_and_links_task_refs() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="inline")
        now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
        execution = _seed_execution(
            session,
            tenant_id=tenant.id,
            task_id=task.id,
            tool_name="nmap",
            started_at=now,
        )
        artifact = _seed_artifact(
            session,
            execution=execution,
            artifact_kind="stdout",
            content_text="artifact text should not replace inline excerpt",
            byte_size=4096,
            mime_type="text/plain",
        )
        evidence = _seed_evidence(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution.id,
            artifact_id=artifact.id,
            inline_excerpt="inline service evidence",
            created_at=now + timedelta(minutes=1),
            lineage={"artifact_kind": "stdout", "target": "10.0.0.5:443"},
            metadata={"type": "terminal"},
            byte_size=1234,
            mime_type="text/plain",
        )
        finding_id = uuid.uuid4()
        provenance = KnowledgeEntityProvenance(
            tenant_id=tenant.id,
            user_id=user.id,
            entity_type="finding",
            entity_id=finding_id,
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution.id,
            tool_name="nmap",
            observed_at=now,
            confidence="high",
            evidence_archive_id=evidence.id,
        )
        session.add(provenance)
        session.flush()

        packet = EvidencePacketBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        assert packet.item_count == 1
        item = packet.items[0]
        assert item.ref == f"evidence_archive:{evidence.id}"
        assert item.evidence_id == str(evidence.id)
        assert item.source_execution_id == str(execution.id)
        assert item.source_artifact_id == str(artifact.id)
        assert item.source_tool == "nmap"
        assert item.evidence_type == "terminal"
        assert item.target == "10.0.0.5:443"
        assert item.excerpt == "inline service evidence"
        assert item.excerpt_source == "inline_excerpt"
        assert item.linked_finding_refs == (f"knowledge_finding:{finding_id}",)
        assert item.byte_size == 1234
        assert item.mime_type == "text/plain"
        assert item.observed_at == now.isoformat()
        assert "artifact text" not in item.summary
    finally:
        session.close()
        engine.dispose()


def test_evidence_packet_is_deterministic_under_repeated_calls() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="deterministic")
        now = datetime(2026, 6, 9, 12, 30, tzinfo=timezone.utc)
        execution = _seed_execution(
            session,
            tenant_id=tenant.id,
            task_id=task.id,
            tool_name="nmap",
            started_at=now,
        )
        artifact = _seed_artifact(
            session,
            execution=execution,
            artifact_kind="stdout",
            content_text="deterministic artifact text",
            byte_size=2048,
            mime_type="text/plain",
        )
        evidence = _seed_evidence(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution.id,
            artifact_id=artifact.id,
            inline_excerpt="deterministic inline evidence",
            created_at=now + timedelta(minutes=1),
            lineage={"artifact_kind": "stdout", "target": "10.0.0.9:443"},
            metadata={"type": "terminal"},
            byte_size=256,
            mime_type="text/plain",
        )
        finding_id = uuid.uuid4()
        provenance = KnowledgeEntityProvenance(
            tenant_id=tenant.id,
            user_id=user.id,
            entity_type="finding",
            entity_id=finding_id,
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution.id,
            tool_name="nmap",
            observed_at=now,
            confidence="high",
            evidence_archive_id=evidence.id,
        )
        session.add(provenance)
        session.flush()

        builder = EvidencePacketBuilder(session)
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


def test_evidence_packet_uses_bounded_artifact_fallback_for_missing_excerpt() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="fallback")
        now = datetime(2026, 6, 9, 13, 0, tzinfo=timezone.utc)
        execution = _seed_execution(
            session,
            tenant_id=tenant.id,
            task_id=task.id,
            tool_name="httpx",
            started_at=now,
        )
        artifact = _seed_artifact(
            session,
            execution=execution,
            artifact_kind="http_response",
            content_text="response body " * 20,
            byte_size=8192,
            mime_type="text/plain",
        )
        _seed_evidence(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=execution.id,
            artifact_id=artifact.id,
            inline_excerpt=None,
            created_at=now,
            lineage={},
            metadata={},
        )

        packet = EvidencePacketBuilder(
            session,
            max_excerpt_characters=40,
            max_total_characters=40,
        ).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )

        item = packet.items[0]
        assert item.excerpt_source == "execution_artifact"
        assert item.excerpt_truncated is True
        assert len(item.excerpt) <= 40
        assert item.source_tool == "httpx"
        assert item.evidence_type == "http_response"
        assert packet.artifact_fallback_count == 1
        assert packet.total_excerpt_characters <= 40
        assert packet.truncated is True
    finally:
        session.close()
        engine.dispose()


def test_evidence_packet_excludes_other_scope_and_handles_unknown_tools() -> None:
    engine, session_factory = _make_session_factory()
    try:
        session = session_factory()
        tenant, user, engagement, task = _seed_scope(session, label="target")
        other_tenant, other_user, other_engagement, other_task = _seed_scope(
            session, label="other"
        )
        now = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
        target_execution_id = uuid.uuid4()
        other_execution_id = uuid.uuid4()
        target = _seed_evidence(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
            execution_id=target_execution_id,
            inline_excerpt="target task evidence",
            created_at=now,
            lineage={},
            metadata={"type": "stdout"},
        )
        _seed_evidence(
            session,
            tenant_id=other_tenant.id,
            user_id=other_user.id,
            engagement_id=other_engagement.id,
            task_id=other_task.id,
            execution_id=other_execution_id,
            inline_excerpt="other task evidence",
            created_at=now,
            lineage={"source_tool": "other"},
            metadata={"type": "stdout"},
        )

        packet = EvidencePacketBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=user.id,
            engagement_id=engagement.id,
            task_id=task.id,
        )
        wrong_scope_packet = EvidencePacketBuilder(session).build_for_task(
            tenant_id=tenant.id,
            user_id=other_user.id,
            engagement_id=other_engagement.id,
            task_id=task.id,
        )

        assert [item.evidence_id for item in packet.items] == [str(target.id)]
        assert packet.items[0].source_tool == "unknown_tool"
        assert packet.items[0].summary.startswith("Evidence archive")
        assert "target task evidence" in packet.items[0].excerpt
        assert wrong_scope_packet.items == ()
    finally:
        session.close()
        engine.dispose()
