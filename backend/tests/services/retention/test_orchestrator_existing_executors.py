"""Tests for registering existing retention executors in the orchestrator.

This module covers the Phase 3 executor registry boundary: knowledge,
artifact, and reporting are wired, while later task/chat/checkpoint/runner,
memory, and usage executors remain outside the existing-maintenance set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.config.workspace_config import WorkspaceConfig
from backend.database import Base
from backend.models.chat import AgentLog
from backend.models.core import Engagement, Task, User
from backend.models.data_management import TenantDataManagementSettings
from backend.models.knowledge import (
    KnowledgeEvidenceArchive,
    KnowledgeFinding,
    KnowledgeIngestionRun,
)
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.reporting import EngagementReport, EngagementReportJob, TaskClosureMemo
from backend.models.streaming import StreamEvent, SystemLog
from backend.services.retention.contracts import (
    RETENTION_CLASS_ARTIFACT_PAYLOAD,
    RETENTION_CLASS_EXECUTION_PROVENANCE,
    RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
    RETENTION_CLASS_REPORTING,
    RETENTION_DECISION_APPLIED,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
    RETENTION_SCOPE_ALL_TENANTS,
    RETENTION_SCOPE_TENANT,
    RetentionBatchCounts,
    RetentionExecutorResult,
    RetentionRunRequest,
)
from backend.models.tenant import Tenant
from backend.services import retention
from backend.services.data_plane.local_object_store import LocalObjectStore
from backend.services.data_plane import retention_service as data_plane_retention
from backend.services.artifact.retention_service import (
    ARTIFACT_PAYLOAD_DELETE_FAILED_REASON,
)
from backend.services.retention import orchestrator as retention_orchestrator
from backend.services.retention.orchestrator import (
    EXISTING_RETENTION_CLASSES,
    EXISTING_RETENTION_EXECUTOR_ORDER,
    build_existing_retention_executors,
)


@dataclass(frozen=True, slots=True)
class _Policy:
    retention_batch_size_per_tenant: int = 10
    operational_log_retention_days: int = 30
    artifact_payload_retention_days: int = 30
    artifact_metadata_retention_days_after_terminal: int = 30
    report_history_retention_days: int = 30


class _FakeSession:
    def __init__(self) -> None:
        self.commit_count = 0
        self.rollback_count = 0

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class _FakeExecutor:
    def __init__(self, *, name: str, retention_class: str) -> None:
        self.name = name
        self.retention_class = retention_class

    def run(self, *, policy, tenant_id: int, mode: str, limit: int):
        return RetentionExecutorResult(
            executor_name=self.name,
            retention_class=self.retention_class,
            mode=mode,
            tenant_id=tenant_id,
            counts=RetentionBatchCounts(batch_limit=limit),
            reason_counts={},
        )


class _DeleteExceptionObjectStore:
    """Test double that raises during deletion for one target object key."""

    def __init__(self, delegate: LocalObjectStore, *, fail_key: str) -> None:
        self._delegate = delegate
        self._fail_key = fail_key

    def delete_object(self, object_key: str) -> bool:
        if object_key == self._fail_key:
            raise RuntimeError("simulated object-store delete failure")
        return self._delegate.delete_object(object_key)

    def head_object(self, object_key: str):
        return self._delegate.head_object(object_key)


def test_existing_executor_factory_registers_only_phase_three_executors() -> None:
    executors = build_existing_retention_executors(_FakeSession())  # type: ignore[arg-type]

    assert [executor.name for executor in executors] == [
        "artifact.retention",
        "artifact_provenance.retention",
        "knowledge.retention",
        "knowledge.evidence_retention",
        "reporting.retention",
    ]
    assert [executor.retention_class for executor in executors] == [
        RETENTION_CLASS_ARTIFACT_PAYLOAD,
        RETENTION_CLASS_EXECUTION_PROVENANCE,
        RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
        RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
        RETENTION_CLASS_REPORTING,
    ]
    assert tuple(entry.executor_name for entry in EXISTING_RETENTION_EXECUTOR_ORDER) == (
        "artifact.retention",
        "artifact_provenance.retention",
        "knowledge.retention",
        "knowledge.evidence_retention",
        "reporting.retention",
    )
    assert EXISTING_RETENTION_CLASSES == (
        RETENTION_CLASS_ARTIFACT_PAYLOAD,
        RETENTION_CLASS_EXECUTION_PROVENANCE,
        RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
        RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
        RETENTION_CLASS_REPORTING,
    )


def test_default_orchestrator_uses_existing_executor_registry(monkeypatch) -> None:
    db = _FakeSession()
    calls: list[str] = []

    def fake_existing_executors(_db):
        return (
            _FakeExecutor(
                name="artifact.retention",
                retention_class=RETENTION_CLASS_ARTIFACT_PAYLOAD,
            ),
            _FakeExecutor(
                name="artifact_provenance.retention",
                retention_class=RETENTION_CLASS_EXECUTION_PROVENANCE,
            ),
            _FakeExecutor(
                name="knowledge.retention",
                retention_class=RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
            ),
            _FakeExecutor(
                name="knowledge.evidence_retention",
                retention_class=RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,
            ),
            _FakeExecutor(
                name="reporting.retention",
                retention_class=RETENTION_CLASS_REPORTING,
            ),
        )

    original_run = _FakeExecutor.run

    def recording_run(self, *, policy, tenant_id: int, mode: str, limit: int):
        calls.append(self.name)
        return original_run(
            self,
            policy=policy,
            tenant_id=tenant_id,
            mode=mode,
            limit=limit,
        )

    monkeypatch.setattr(
        retention_orchestrator,
        "build_existing_retention_executors",
        fake_existing_executors,
    )
    monkeypatch.setattr(_FakeExecutor, "run", recording_run)

    result = retention_orchestrator.RetentionOrchestrator(
        db,  # type: ignore[arg-type]
        executor_order=EXISTING_RETENTION_EXECUTOR_ORDER,
        policy_resolver=lambda _db, tenant_id: _Policy(),  # type: ignore[arg-type, return-value]
    ).run(
        RetentionRunRequest(
            mode=RETENTION_RUN_MODE_DRY_RUN,
            scope=RETENTION_SCOPE_TENANT,
            tenant_id=1,
            retention_classes=EXISTING_RETENTION_CLASSES,
        )
    )

    assert result.succeeded is True
    assert calls == [
        "artifact.retention",
        "artifact_provenance.retention",
        "knowledge.retention",
        "knowledge.evidence_retention",
        "reporting.retention",
    ]
    assert db.commit_count == 0
    assert db.rollback_count == 5


def test_default_orchestrator_routes_operational_ephemeral_to_knowledge_executor() -> None:
    db = _build_retention_session()
    try:
        tenant_id = _seed_tenant_operational_log_rows(db)
        db.commit()

        result = retention_orchestrator.RetentionOrchestrator(db).run(
            RetentionRunRequest(
                mode=RETENTION_RUN_MODE_APPLY,
                scope=RETENTION_SCOPE_TENANT,
                tenant_id=tenant_id,
                retention_classes=(RETENTION_CLASS_OPERATIONAL_EPHEMERAL,),
            )
        )

        knowledge_results = [
            item
            for item in result.results
            if item.executor_name == "knowledge.retention"
        ]
        assert len(knowledge_results) == 1
        assert knowledge_results[0].retention_class == RETENTION_CLASS_OPERATIONAL_EPHEMERAL
        assert knowledge_results[0].counts.applied_count == 1
        assert db.query(AgentLog).filter(AgentLog.tenant_id == tenant_id).count() == 0
    finally:
        db.close()


def test_default_orchestrator_does_not_run_operational_deletion_for_engagement_knowledge() -> None:
    db = _build_retention_session()
    try:
        tenant_id = _seed_tenant_operational_log_rows(db)
        db.commit()

        result = retention_orchestrator.RetentionOrchestrator(db).run(
            RetentionRunRequest(
                mode=RETENTION_RUN_MODE_APPLY,
                scope=RETENTION_SCOPE_TENANT,
                tenant_id=tenant_id,
                retention_classes=(RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,),
            )
        )

        assert [item.executor_name for item in result.results] == [
            "knowledge.evidence_retention"
        ]
        assert result.results[0].retention_class == RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE
        assert result.results[0].counts.applied_count == 0
        assert db.query(AgentLog).filter(AgentLog.tenant_id == tenant_id).count() == 1
    finally:
        db.close()


def test_existing_orchestrator_counts_only_artifact_executor_object_deletions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _build_retention_session()
    store = LocalObjectStore(root_path=tmp_path / "object-store")
    monkeypatch.setattr(data_plane_retention, "get_object_store", lambda: store)
    try:
        tenant_ids = []
        keys_by_tenant: dict[int, list[str]] = {}
        evidence_paths: list[Path] = []
        for tenant_index in (1, 2):
            tenant_id, object_keys, archived_paths = _seed_tenant_artifact_evidence_rows(
                db,
                store=store,
                evidence_root=tmp_path / f"tenant-{tenant_index}-evidence",
                tenant_index=tenant_index,
            )
            tenant_ids.append(tenant_id)
            keys_by_tenant[tenant_id] = object_keys
            evidence_paths.extend(archived_paths)
        db.commit()

        result = retention_orchestrator.RetentionOrchestrator(
            db,
            executor_order=EXISTING_RETENTION_EXECUTOR_ORDER,
            policy_resolver=lambda _db, tenant_id: _Policy(
                retention_batch_size_per_tenant=1
            ),
        ).run(
            RetentionRunRequest(
                mode=RETENTION_RUN_MODE_APPLY,
                scope=RETENTION_SCOPE_ALL_TENANTS,
                retention_classes=(
                    RETENTION_CLASS_ARTIFACT_PAYLOAD,
                    RETENTION_CLASS_OPERATIONAL_EPHEMERAL,
                    RETENTION_CLASS_REPORTING,
                ),
                limit_per_tenant=1,
            )
        )

        assert result.succeeded is True
        artifact_results = [
            item
            for item in result.results
            if item.executor_name == "artifact.retention"
        ]
        knowledge_results = [
            item
            for item in result.results
            if item.executor_name == "knowledge.retention"
        ]
        reporting_results = [
            item
            for item in result.results
            if item.executor_name == "reporting.retention"
        ]

        assert len(artifact_results) == len(tenant_ids)
        assert all(item.counts.applied_count == 1 for item in artifact_results)
        assert all(item.counts.batch_limit == 1 for item in artifact_results)
        assert all(item.counts.applied_count == 0 for item in knowledge_results)
        assert all(item.counts.applied_count == 0 for item in reporting_results)

        actual_deleted_count = 0
        for tenant_id in tenant_ids:
            deleted_for_tenant = sum(
                1 for object_key in keys_by_tenant[tenant_id] if store.head_object(object_key) is None
            )
            assert deleted_for_tenant == 1
            actual_deleted_count += deleted_for_tenant

        safe_applied_count = sum(item.counts.applied_count for item in result.results)
        assert safe_applied_count == actual_deleted_count == len(tenant_ids)
        assert all(path.exists() for path in evidence_paths)
        assert (
            db.query(KnowledgeEvidenceArchive)
            .filter(KnowledgeEvidenceArchive.storage_mode == "archived_file")
            .count()
            == 4
        )
    finally:
        db.close()


def test_scheduled_existing_retention_compacts_target_tenant_evidence_only() -> None:
    db = _build_retention_session()
    try:
        first = _seed_tenant_evidence_compaction_rows(
            db,
            tenant_index=1,
        )
        second = _seed_tenant_evidence_compaction_rows(
            db,
            tenant_index=2,
        )
        db.commit()

        result = retention_orchestrator.RetentionOrchestrator(
            db,
            executor_order=EXISTING_RETENTION_EXECUTOR_ORDER,
            policy_resolver=lambda _db, tenant_id: _Policy(
                retention_batch_size_per_tenant=10
            ),
        ).run(
            RetentionRunRequest(
                mode=RETENTION_RUN_MODE_APPLY,
                scope=RETENTION_SCOPE_TENANT,
                tenant_id=first["tenant_id"],
                retention_classes=(RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE,),
            )
        )

        assert result.succeeded is True
        knowledge_results = [
            item
            for item in result.results
            if item.executor_name == "knowledge.evidence_retention"
        ]
        assert len(knowledge_results) == 1
        evidence_result = knowledge_results[0]
        assert evidence_result.retention_class == RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE
        assert evidence_result.counts.candidate_count == 1
        assert evidence_result.counts.applied_count == 1
        assert {
            decision.retention_class for decision in evidence_result.decisions
        } == {RETENTION_CLASS_ENGAGEMENT_KNOWLEDGE}
        assert any(
            decision.outcome == RETENTION_DECISION_APPLIED
            for decision in evidence_result.decisions
        )

        target_cold = db.get(KnowledgeEvidenceArchive, first["cold_evidence_id"])
        target_protected = db.get(
            KnowledgeEvidenceArchive,
            first["protected_evidence_id"],
        )
        foreign_cold = db.get(KnowledgeEvidenceArchive, second["cold_evidence_id"])
        assert target_cold is not None
        assert target_protected is not None
        assert foreign_cold is not None
        assert str(target_cold.storage_mode) == "metadata_only"
        assert target_cold.archived_file_ref is None
        assert first["cold_path"].exists() is False
        assert str(target_protected.storage_mode) == "archived_file"
        assert target_protected.archived_file_ref == str(first["protected_path"].resolve())
        assert first["protected_path"].exists() is True
        assert str(foreign_cold.storage_mode) == "archived_file"
        assert foreign_cold.archived_file_ref == str(second["cold_path"].resolve())
        assert second["cold_path"].exists() is True
    finally:
        db.close()


def test_cleanup_agent_logs_returns_zero_and_rolls_back_artifact_delete_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = _build_retention_session()
    base_store = LocalObjectStore(root_path=tmp_path / "object-store")
    try:
        tenant_id, object_keys, _archived_paths = _seed_tenant_artifact_evidence_rows(
            db,
            store=base_store,
            evidence_root=tmp_path / "tenant-artifact-evidence",
            tenant_index=1,
            evidence_storage_mode="metadata_only",
            row_indices=(1,),
        )
        fail_key = object_keys[0]
        monkeypatch.setattr(
            data_plane_retention,
            "get_object_store",
            lambda: _DeleteExceptionObjectStore(base_store, fail_key=fail_key),
        )
        db.commit()

        deleted = retention.cleanup_agent_logs(db)

        assert deleted == 0
        assert ARTIFACT_PAYLOAD_DELETE_FAILED_REASON in caplog.text
        assert fail_key not in caplog.text
        artifact = (
            db.query(ExecutionArtifact)
            .filter(
                ExecutionArtifact.tenant_id == tenant_id,
                ExecutionArtifact.object_key == fail_key,
            )
            .one()
        )
        assert artifact.object_key == fail_key
        assert dict(artifact.artifact_metadata or {}).get("retention") is None
        assert base_store.head_object(fail_key) is not None
    finally:
        db.close()


def _build_retention_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Engagement.__table__,
            Task.__table__,
            TenantDataManagementSettings.__table__,
            AgentLog.__table__,
            SystemLog.__table__,
            StreamEvent.__table__,
            ToolExecution.__table__,
            ExecutionArtifact.__table__,
            KnowledgeEvidenceArchive.__table__,
            KnowledgeFinding.__table__,
            KnowledgeIngestionRun.__table__,
            EngagementReport.__table__,
            EngagementReportJob.__table__,
            TaskClosureMemo.__table__,
        ],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_tenant_operational_log_rows(db: Session) -> int:
    tenant = Tenant(slug=f"tenant-{uuid.uuid4().hex}", name="Tenant")
    user = User(
        username=f"operational-user-{uuid.uuid4().hex}",
        password="test-password",
        email=f"operational-{uuid.uuid4().hex}@example.com",
    )
    db.add_all([tenant, user])
    db.flush()

    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant.id,
        name="Operational Retention Engagement",
    )
    db.add(engagement)
    db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        engagement_id=engagement.id,
        name="Operational Retention Task",
    )
    db.add(task)
    db.flush()

    db.add(
        TenantDataManagementSettings(
            tenant_id=tenant.id,
            operational_log_retention_days=30,
            retention_batch_size_per_tenant=10,
        )
    )
    db.add(
        AgentLog(
            task_id=task.id,
            tenant_id=tenant.id,
            sequence=1,
            type="reasoning",
            content="old operational log",
            turn_id="turn-1",
            turn_number=1,
            timestamp=datetime.now(tz=UTC) - timedelta(days=45),
        )
    )
    return int(tenant.id)


def _seed_tenant_artifact_evidence_rows(
    db: Session,
    *,
    store: LocalObjectStore,
    evidence_root: Path,
    tenant_index: int,
    evidence_storage_mode: str = "archived_file",
    row_indices: tuple[int, ...] = (1, 2),
) -> tuple[int, list[str], list[Path]]:
    tenant = Tenant(
        slug=f"tenant-{tenant_index}-{uuid.uuid4().hex}",
        name=f"Tenant {tenant_index}",
    )
    db.add(tenant)
    db.flush()

    user = User(
        username=f"retention-user-{tenant_index}-{uuid.uuid4().hex}",
        password="test-password",
        email=f"retention-{tenant_index}-{uuid.uuid4().hex}@example.com",
    )
    db.add(user)
    db.flush()

    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant.id,
        name=f"Retention Engagement {tenant_index}",
    )
    db.add(engagement)
    db.flush()

    object_keys: list[str] = []
    archived_paths: list[Path] = []
    evidence_root.mkdir(parents=True, exist_ok=True)
    for row_index in row_indices:
        task = Task(
            user_id=user.id,
            tenant_id=tenant.id,
            engagement_id=engagement.id,
            name=f"Retention Task {tenant_index}-{row_index}",
        )
        db.add(task)
        db.flush()

        execution = ToolExecution(
            tenant_id=tenant.id,
            task_id=task.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo retained"},
            agent_path="runner.tool_command",
            status="succeeded",
            started_at=datetime.now(tz=UTC),
        )
        db.add(execution)
        db.flush()

        object_key = (
            f"tenants/{tenant.id}/tasks/{task.id}/executions/{execution.id}/"
            f"artifacts/{row_index}/output.txt"
        )
        payload = f"payload-{tenant_index}-{row_index}".encode()
        store.put_bytes(object_key, payload, content_type="text/plain")
        artifact = ExecutionArtifact(
            execution_id=execution.id,
            tenant_id=tenant.id,
            task_id=task.id,
            artifact_kind="tool_result",
            object_key=object_key,
            upload_status="ready",
            byte_size=len(payload),
            mime_type="text/plain",
            is_text=True,
            artifact_metadata={},
            created_at=datetime(2025, 1, row_index, tzinfo=UTC),
        )
        db.add(artifact)
        db.flush()

        evidence_path = evidence_root / f"evidence-{row_index}.bin"
        evidence_path.write_bytes(b"archived-evidence")
        db.add(
            KnowledgeEvidenceArchive(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                user_id=user.id,
                engagement_id=engagement.id,
                task_id=task.id,
                source_execution_id=execution.id,
                source_artifact_id=artifact.id,
                storage_mode=evidence_storage_mode,
                inline_excerpt="cold evidence",
                archived_file_ref=str(evidence_path),
                lineage_snapshot={"artifact_id": str(artifact.id)},
                archive_metadata={},
            )
        )
        object_keys.append(object_key)
        archived_paths.append(evidence_path)

    return int(tenant.id), object_keys, archived_paths


def _seed_tenant_evidence_compaction_rows(
    db: Session,
    *,
    tenant_index: int,
) -> dict[str, object]:
    tenant = Tenant(
        slug=f"evidence-tenant-{tenant_index}-{uuid.uuid4().hex}",
        name=f"Evidence Tenant {tenant_index}",
    )
    db.add(tenant)
    db.flush()

    user = User(
        username=f"evidence-user-{tenant_index}-{uuid.uuid4().hex}",
        password="test-password",
        email=f"evidence-{tenant_index}-{uuid.uuid4().hex}@example.com",
    )
    db.add(user)
    db.flush()

    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant.id,
        name=f"Evidence Engagement {tenant_index}",
    )
    db.add(engagement)
    db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        engagement_id=engagement.id,
        name=f"Evidence Task {tenant_index}",
    )
    db.add(task)
    db.flush()

    evidence_dir = WorkspaceConfig.ensure_engagement_durable_structure(
        engagement.id
    )["evidence"]
    cold_path = evidence_dir / f"cold-{tenant_index}.bin"
    protected_path = evidence_dir / f"protected-{tenant_index}.bin"
    cold_path.write_bytes(b"cold evidence")
    protected_path.write_bytes(b"protected evidence")

    cold_evidence = KnowledgeEvidenceArchive(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        source_execution_id=uuid.uuid4(),
        source_artifact_id=uuid.uuid4(),
        storage_mode="archived_file",
        inline_excerpt="cold",
        archived_file_ref=str(cold_path.resolve()),
        lineage_snapshot={"artifact_id": f"cold-{tenant_index}"},
        archive_metadata={"policy_family": "default_archive_policy"},
    )
    protected_evidence = KnowledgeEvidenceArchive(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        source_execution_id=uuid.uuid4(),
        source_artifact_id=uuid.uuid4(),
        storage_mode="archived_file",
        inline_excerpt="protected",
        archived_file_ref=str(protected_path.resolve()),
        lineage_snapshot={"artifact_id": f"protected-{tenant_index}"},
        archive_metadata={"delete_survival_required": True},
    )
    db.add_all([cold_evidence, protected_evidence])

    return {
        "tenant_id": int(tenant.id),
        "cold_evidence_id": cold_evidence.id,
        "protected_evidence_id": protected_evidence.id,
        "cold_path": cold_path,
        "protected_path": protected_path,
    }
