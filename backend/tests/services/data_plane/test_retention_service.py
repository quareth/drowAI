"""Tests for task-scoped data-plane artifact object retention.

This module verifies dry-run safety, idempotent object deletion, and protected
artifact preservation for retention policy execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.knowledge import KnowledgeEvidenceArchive, KnowledgeFinding, KnowledgeIngestionRun
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.tenant import Tenant
from backend.services.artifact.retention_service import ArtifactRetentionExecutor
from backend.services.data_plane.local_object_store import LocalObjectStore
from backend.services.data_plane.retention_service import DataPlaneRetentionService
from backend.services.retention.contracts import (
    RETENTION_CLASS_ARTIFACT_PAYLOAD,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_DECISION_FAILED,
    RETENTION_DECISION_PROTECTED,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
)


@dataclass(frozen=True, slots=True)
class _ArtifactPolicy:
    artifact_payload_retention_days: int = 30
    retention_batch_size_per_tenant: int = 100


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


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Engagement.__table__,
            Task.__table__,
            ToolExecution.__table__,
            ExecutionArtifact.__table__,
            KnowledgeEvidenceArchive.__table__,
            KnowledgeFinding.__table__,
            KnowledgeIngestionRun.__table__,
        ],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_execution_artifact(
    db: Session,
    *,
    object_key: str,
    payload: bytes,
    created_at: datetime | None = None,
) -> ExecutionArtifact:
    unique_suffix = uuid.uuid4().hex
    tenant = Tenant(slug=f"tenant-{unique_suffix}", name="Tenant")
    db.add(tenant)
    db.flush()

    user = User(
        username=f"user-{unique_suffix}",
        password="test-password",
        email=f"{unique_suffix}@example.com",
    )
    db.add(user)
    db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        name=f"Retention Task {unique_suffix}",
    )
    db.add(task)
    db.flush()

    execution = ToolExecution(
        tenant_id=tenant.id,
        task_id=task.id,
        tool_name="shell.exec",
        tool_arguments={"command": "echo hello"},
        agent_path="runner.tool_command",
        status="succeeded",
        started_at=datetime.now(tz=UTC),
    )
    db.add(execution)
    db.flush()

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
    )
    if created_at is not None:
        artifact.created_at = created_at
    db.add(artifact)
    db.commit()
    return artifact


def _seed_engagement_artifact(
    db: Session,
    *,
    object_key: str,
    payload: bytes,
    user: User | None = None,
    engagement: Engagement | None = None,
    task: Task | None = None,
    created_at: datetime | None = None,
) -> tuple[User, Engagement, Task, ToolExecution, ExecutionArtifact]:
    unique_suffix = uuid.uuid4().hex
    if user is None or engagement is None:
        tenant = Tenant(slug=f"tenant-{unique_suffix}", name="Tenant")
        db.add(tenant)
        db.flush()

        user = User(
            username=f"user-{unique_suffix}",
            password="test-password",
            email=f"{unique_suffix}@example.com",
        )
        db.add(user)
        db.flush()

        engagement = Engagement(
            user_id=user.id,
            tenant_id=tenant.id,
            name=f"Retention Engagement {unique_suffix}",
        )
        db.add(engagement)
        db.flush()

    assert user is not None
    assert engagement is not None

    if task is None:
        task = Task(
            user_id=user.id,
            tenant_id=engagement.tenant_id,
            engagement_id=engagement.id,
            name=f"Retention Task {unique_suffix}",
        )
        db.add(task)
        db.flush()

    execution = ToolExecution(
        tenant_id=engagement.tenant_id,
        task_id=task.id,
        tool_name="shell.exec",
        tool_arguments={"command": "echo hello"},
        agent_path="runner.tool_command",
        status="succeeded",
        started_at=datetime.now(tz=UTC),
    )
    db.add(execution)
    db.flush()

    artifact = ExecutionArtifact(
        execution_id=execution.id,
        tenant_id=engagement.tenant_id,
        task_id=task.id,
        artifact_kind="tool_result",
        object_key=object_key,
        upload_status="ready",
        byte_size=len(payload),
        mime_type="text/plain",
        is_text=True,
        artifact_metadata={},
    )
    if created_at is not None:
        artifact.created_at = created_at
    db.add(artifact)
    db.flush()
    return user, engagement, task, execution, artifact


def _add_evidence_archive(
    db: Session,
    *,
    user: User,
    engagement: Engagement,
    task: Task,
    execution: ToolExecution,
    artifact: ExecutionArtifact,
    delete_survival_required: bool = False,
) -> KnowledgeEvidenceArchive:
    evidence = KnowledgeEvidenceArchive(
        id=uuid.uuid4(),
        tenant_id=engagement.tenant_id,
        user_id=user.id,
        engagement_id=engagement.id,
        task_id=task.id,
        source_execution_id=execution.id,
        source_artifact_id=artifact.id,
        storage_mode="archived_file",
        inline_excerpt="safe excerpt",
        archived_file_ref="/tmp/not-used",
        lineage_snapshot={"artifact_id": str(artifact.id)},
        archive_metadata={"delete_survival_required": True}
        if delete_survival_required
        else {},
    )
    db.add(evidence)
    db.flush()
    return evidence


def test_artifact_object_retention_dry_run_keeps_objects(tmp_path: Path) -> None:
    db = _build_session()
    store = LocalObjectStore(root_path=tmp_path / "object-store")
    payload = b"dry-run-payload"
    object_key = "tenants/1/tasks/1/executions/e1/artifacts/a1/output.txt"
    store.put_bytes(object_key, payload, content_type="text/plain")
    artifact = _seed_execution_artifact(db, object_key=object_key, payload=payload)

    service = DataPlaneRetentionService(db, object_store=store)
    result = service.run_artifact_object_retention(
        tenant_task_scopes={(int(artifact.tenant_id), int(artifact.task_id))},
        archived_artifact_ids={str(artifact.id)},
        protected_artifact_ids=set(),
        dry_run=True,
    )
    summary = result.to_dict()

    assert summary["dry_run"] is True
    assert summary["candidate_count"] == 1
    assert summary["deleted_count"] == 0
    assert summary["estimated_delete_bytes"] == len(payload)
    assert "object_key" not in summary["eligible"][0]
    assert store.head_object(object_key) is not None
    refreshed = db.query(ExecutionArtifact).filter(ExecutionArtifact.id == artifact.id).one()
    assert str(refreshed.object_key) == object_key


def test_artifact_object_retention_delete_is_idempotent(tmp_path: Path) -> None:
    db = _build_session()
    store = LocalObjectStore(root_path=tmp_path / "object-store")
    payload = b"delete-me"
    object_key = "tenants/1/tasks/2/executions/e2/artifacts/a2/output.txt"
    store.put_bytes(object_key, payload, content_type="text/plain")
    artifact = _seed_execution_artifact(db, object_key=object_key, payload=payload)

    service = DataPlaneRetentionService(db, object_store=store)
    first = service.run_artifact_object_retention(
        tenant_task_scopes={(int(artifact.tenant_id), int(artifact.task_id))},
        archived_artifact_ids={str(artifact.id)},
        protected_artifact_ids=set(),
        dry_run=False,
    ).to_dict()
    db.commit()
    assert first["deleted_count"] == 1
    assert store.head_object(object_key) is None

    second = service.run_artifact_object_retention(
        tenant_task_scopes={(int(artifact.tenant_id), int(artifact.task_id))},
        archived_artifact_ids={str(artifact.id)},
        protected_artifact_ids=set(),
        dry_run=False,
    ).to_dict()
    db.commit()
    assert second["deleted_count"] == 0
    assert second["candidate_count"] == 0


def test_artifact_object_retention_preserves_protected_artifacts(tmp_path: Path) -> None:
    db = _build_session()
    store = LocalObjectStore(root_path=tmp_path / "object-store")
    payload = b"must-stay"
    object_key = "tenants/1/tasks/3/executions/e3/artifacts/a3/output.txt"
    store.put_bytes(object_key, payload, content_type="text/plain")
    artifact = _seed_execution_artifact(db, object_key=object_key, payload=payload)

    service = DataPlaneRetentionService(db, object_store=store)
    result = service.run_artifact_object_retention(
        tenant_task_scopes={(int(artifact.tenant_id), int(artifact.task_id))},
        archived_artifact_ids={str(artifact.id)},
        protected_artifact_ids={str(artifact.id)},
        dry_run=False,
    ).to_dict()
    db.commit()

    assert result["candidate_count"] == 0
    assert result["deleted_count"] == 0
    assert result["preserved_count"] == 1
    assert result["preserved"][0]["reason"] == "durable_evidence_policy_protected"
    assert store.head_object(object_key) is not None


def test_artifact_object_retention_delete_exception_preserves_object_key(tmp_path: Path) -> None:
    db = _build_session()
    base_store = LocalObjectStore(root_path=tmp_path / "object-store")
    payload = b"retry-me"
    object_key = "tenants/1/tasks/4/executions/e4/artifacts/a4/output.txt"
    base_store.put_bytes(object_key, payload, content_type="text/plain")
    artifact = _seed_execution_artifact(db, object_key=object_key, payload=payload)
    failing_store = _DeleteExceptionObjectStore(base_store, fail_key=object_key)

    service = DataPlaneRetentionService(db, object_store=failing_store)
    result = service.run_artifact_object_retention(
        tenant_task_scopes={(int(artifact.tenant_id), int(artifact.task_id))},
        archived_artifact_ids={str(artifact.id)},
        protected_artifact_ids=set(),
        dry_run=False,
    ).to_dict()
    db.commit()

    assert result["candidate_count"] == 1
    assert result["deleted_count"] == 0
    assert result["already_deleted_count"] == 0

    refreshed = db.query(ExecutionArtifact).filter(ExecutionArtifact.id == artifact.id).one()
    assert str(refreshed.object_key) == object_key
    retention_meta = dict((refreshed.artifact_metadata or {}).get("retention") or {})
    assert retention_meta["object_deleted"] is False
    assert retention_meta["delete_status"] == "delete_failed"
    assert "RuntimeError" in str(retention_meta.get("delete_error"))
    assert base_store.head_object(object_key) is not None


def test_artifact_object_retention_delete_does_not_touch_unlisted_tenant_rows(tmp_path: Path) -> None:
    db = _build_session()
    store = LocalObjectStore(root_path=tmp_path / "object-store")

    keep_payload = b"keep-tenant-b"
    delete_payload = b"delete-tenant-a"
    keep_object_key = "tenants/2/tasks/22/executions/e22/artifacts/a22/output.txt"
    delete_object_key = "tenants/1/tasks/11/executions/e11/artifacts/a11/output.txt"
    store.put_bytes(keep_object_key, keep_payload, content_type="text/plain")
    store.put_bytes(delete_object_key, delete_payload, content_type="text/plain")

    keep_artifact = _seed_execution_artifact(
        db,
        object_key=keep_object_key,
        payload=keep_payload,
    )
    delete_artifact = _seed_execution_artifact(
        db,
        object_key=delete_object_key,
        payload=delete_payload,
    )

    service = DataPlaneRetentionService(db, object_store=store)
    result = service.run_artifact_object_retention(
        tenant_task_scopes={(int(delete_artifact.tenant_id), int(delete_artifact.task_id))},
        archived_artifact_ids={str(delete_artifact.id)},
        protected_artifact_ids=set(),
        dry_run=False,
    ).to_dict()
    db.commit()

    assert result["deleted_count"] == 1
    assert store.head_object(delete_object_key) is None
    assert store.head_object(keep_object_key) is not None

    retained_row = db.query(ExecutionArtifact).filter(ExecutionArtifact.id == keep_artifact.id).one()
    deleted_row = db.query(ExecutionArtifact).filter(ExecutionArtifact.id == delete_artifact.id).one()
    assert str(retained_row.object_key) == keep_object_key
    assert deleted_row.object_key is None


def test_artifact_object_retention_ignores_foreign_artifact_ids_outside_scope(tmp_path: Path) -> None:
    db = _build_session()
    store = LocalObjectStore(root_path=tmp_path / "object-store")

    local_payload = b"local"
    foreign_payload = b"foreign"
    local_object_key = "tenants/1/tasks/31/executions/e31/artifacts/a31/output.txt"
    foreign_object_key = "tenants/2/tasks/41/executions/e41/artifacts/a41/output.txt"
    store.put_bytes(local_object_key, local_payload, content_type="text/plain")
    store.put_bytes(foreign_object_key, foreign_payload, content_type="text/plain")

    local_artifact = _seed_execution_artifact(
        db,
        object_key=local_object_key,
        payload=local_payload,
    )
    foreign_artifact = _seed_execution_artifact(
        db,
        object_key=foreign_object_key,
        payload=foreign_payload,
    )

    service = DataPlaneRetentionService(db, object_store=store)
    result = service.run_artifact_object_retention(
        tenant_task_scopes={(int(local_artifact.tenant_id), int(local_artifact.task_id))},
        archived_artifact_ids={str(foreign_artifact.id)},
        protected_artifact_ids=set(),
        dry_run=False,
    ).to_dict()
    db.commit()

    assert result["deleted_count"] == 0
    assert store.head_object(local_object_key) is not None
    assert store.head_object(foreign_object_key) is not None


def test_artifact_retention_executor_reports_canonical_counts_without_object_keys(tmp_path: Path) -> None:
    db = _build_session()
    store = LocalObjectStore(root_path=tmp_path / "object-store")
    expired_at = datetime.now(tz=UTC) - timedelta(days=31)
    candidate_key = "tenants/1/tasks/51/executions/e51/artifacts/a51/output.txt"
    protected_key = "tenants/1/tasks/52/executions/e52/artifacts/a52/output.txt"
    store.put_bytes(candidate_key, b"candidate", content_type="text/plain")
    store.put_bytes(protected_key, b"protected", content_type="text/plain")
    user, engagement, task, execution, candidate = _seed_engagement_artifact(
        db,
        object_key=candidate_key,
        payload=b"candidate",
        created_at=expired_at,
    )
    _add_evidence_archive(
        db,
        user=user,
        engagement=engagement,
        task=task,
        execution=execution,
        artifact=candidate,
    )
    _same_user, _same_engagement, protected_task, protected_execution, protected = (
        _seed_engagement_artifact(
            db,
            object_key=protected_key,
            payload=b"protected",
            user=user,
            engagement=engagement,
            created_at=expired_at,
        )
    )
    protected_evidence = _add_evidence_archive(
        db,
        user=user,
        engagement=engagement,
        task=protected_task,
        execution=protected_execution,
        artifact=protected,
    )
    db.add(
        KnowledgeFinding(
            id=uuid.uuid4(),
            tenant_id=engagement.tenant_id,
            user_id=user.id,
            engagement_id=engagement.id,
            finding_key=f"finding://active/{uuid.uuid4()}",
            finding_type="vulnerability",
            subject_type="finding.instance",
            subject_key="finding.instance:active-artifact",
            title="Active finding",
            severity="high",
            status="open",
            assertion_level="observed",
            confidence="high",
            first_seen_at=datetime.now(tz=UTC),
            last_seen_at=datetime.now(tz=UTC),
            evidence_summary={
                "evidence_refs": [
                    {"evidence_archive_id": str(protected_evidence.id)}
                ]
            },
        )
    )
    db.commit()

    result = ArtifactRetentionExecutor(
        db,
        data_plane_retention_service=DataPlaneRetentionService(db, object_store=store),
    ).run(
        policy=_ArtifactPolicy(retention_batch_size_per_tenant=10),
        tenant_id=engagement.tenant_id,
        mode=RETENTION_RUN_MODE_DRY_RUN,
        limit=10,
    )
    safe_summary = result.to_safe_dict()

    assert result.retention_class == RETENTION_CLASS_ARTIFACT_PAYLOAD
    assert result.counts.candidate_count == 1
    assert result.counts.protected_count == 1
    assert result.counts.preserved_count == 1
    assert result.counts.applied_count == 0
    assert {decision.outcome for decision in result.decisions} == {
        RETENTION_DECISION_CANDIDATE,
        RETENTION_DECISION_PROTECTED,
    }
    assert candidate_key not in str(safe_summary)
    assert protected_key not in str(safe_summary)
    assert store.head_object(candidate_key) is not None
    assert store.head_object(protected_key) is not None


def test_artifact_retention_executor_uses_payload_retention_window_for_candidates(tmp_path: Path) -> None:
    db = _build_session()
    store = LocalObjectStore(root_path=tmp_path / "object-store")
    expired_at = datetime.now(tz=UTC) - timedelta(days=31)
    recent_at = datetime.now(tz=UTC) - timedelta(days=5)
    expired_key = "tenants/1/tasks/81/executions/e81/artifacts/a81/output.txt"
    recent_key = "tenants/1/tasks/82/executions/e82/artifacts/a82/output.txt"
    store.put_bytes(expired_key, b"expired", content_type="text/plain")
    store.put_bytes(recent_key, b"recent", content_type="text/plain")
    user, engagement, task, execution, expired_artifact = _seed_engagement_artifact(
        db,
        object_key=expired_key,
        payload=b"expired",
        created_at=expired_at,
    )
    _add_evidence_archive(
        db,
        user=user,
        engagement=engagement,
        task=task,
        execution=execution,
        artifact=expired_artifact,
    )
    _same_user, _same_engagement, recent_task, recent_execution, recent_artifact = (
        _seed_engagement_artifact(
            db,
            object_key=recent_key,
            payload=b"recent",
            user=user,
            engagement=engagement,
            created_at=recent_at,
        )
    )
    _add_evidence_archive(
        db,
        user=user,
        engagement=engagement,
        task=recent_task,
        execution=recent_execution,
        artifact=recent_artifact,
    )
    db.commit()

    executor = ArtifactRetentionExecutor(
        db,
        data_plane_retention_service=DataPlaneRetentionService(db, object_store=store),
    )
    dry_run = executor.run(
        policy=_ArtifactPolicy(artifact_payload_retention_days=30, retention_batch_size_per_tenant=10),
        tenant_id=engagement.tenant_id,
        mode=RETENTION_RUN_MODE_DRY_RUN,
        limit=10,
    )
    apply_result = executor.run(
        policy=_ArtifactPolicy(artifact_payload_retention_days=30, retention_batch_size_per_tenant=10),
        tenant_id=engagement.tenant_id,
        mode=RETENTION_RUN_MODE_APPLY,
        limit=10,
    )
    db.commit()

    assert dry_run.counts.scanned_count == apply_result.counts.scanned_count == 1
    assert dry_run.counts.candidate_count == apply_result.counts.candidate_count == 1
    assert dry_run.counts.applied_count == 0
    assert apply_result.counts.applied_count == 1
    assert store.head_object(expired_key) is None
    assert store.head_object(recent_key) is not None
    refreshed_recent = db.query(ExecutionArtifact).filter(ExecutionArtifact.id == recent_artifact.id).one()
    assert str(refreshed_recent.object_key) == recent_key
    assert expired_key not in str(dry_run.to_safe_dict())
    assert recent_key not in str(dry_run.to_safe_dict())
    assert expired_key not in str(apply_result.to_safe_dict())
    assert recent_key not in str(apply_result.to_safe_dict())


def test_artifact_retention_executor_limits_object_deletions_per_tenant(tmp_path: Path) -> None:
    db = _build_session()
    store = LocalObjectStore(root_path=tmp_path / "object-store")
    expired_at = datetime.now(tz=UTC) - timedelta(days=31)
    first_key = "tenants/1/tasks/61/executions/e61/artifacts/a61/output.txt"
    second_key = "tenants/1/tasks/62/executions/e62/artifacts/a62/output.txt"
    store.put_bytes(first_key, b"first", content_type="text/plain")
    store.put_bytes(second_key, b"second", content_type="text/plain")
    user, engagement, task, execution, first_artifact = _seed_engagement_artifact(
        db,
        object_key=first_key,
        payload=b"first",
        created_at=expired_at,
    )
    _add_evidence_archive(
        db,
        user=user,
        engagement=engagement,
        task=task,
        execution=execution,
        artifact=first_artifact,
    )
    _same_user, _same_engagement, second_task, second_execution, second_artifact = (
        _seed_engagement_artifact(
            db,
            object_key=second_key,
            payload=b"second",
            user=user,
            engagement=engagement,
            created_at=expired_at,
        )
    )
    _add_evidence_archive(
        db,
        user=user,
        engagement=engagement,
        task=second_task,
        execution=second_execution,
        artifact=second_artifact,
    )
    db.commit()

    result = ArtifactRetentionExecutor(
        db,
        data_plane_retention_service=DataPlaneRetentionService(db, object_store=store),
    ).run(
        policy=_ArtifactPolicy(retention_batch_size_per_tenant=1),
        tenant_id=engagement.tenant_id,
        mode=RETENTION_RUN_MODE_APPLY,
        limit=10,
    )
    db.commit()

    assert result.counts.candidate_count == 1
    assert result.counts.applied_count == 1
    assert result.counts.already_deleted_count == 0
    assert result.counts.failed_count == 0
    assert result.counts.batch_limit == 1
    assert sum(
        1
        for key in (first_key, second_key)
        if store.head_object(key) is None
    ) == 1


def test_artifact_retention_executor_reports_delete_failures(tmp_path: Path) -> None:
    db = _build_session()
    base_store = LocalObjectStore(root_path=tmp_path / "object-store")
    expired_at = datetime.now(tz=UTC) - timedelta(days=31)
    object_key = "tenants/1/tasks/71/executions/e71/artifacts/a71/output.txt"
    base_store.put_bytes(object_key, b"fail", content_type="text/plain")
    user, engagement, task, execution, artifact = _seed_engagement_artifact(
        db,
        object_key=object_key,
        payload=b"fail",
        created_at=expired_at,
    )
    _add_evidence_archive(
        db,
        user=user,
        engagement=engagement,
        task=task,
        execution=execution,
        artifact=artifact,
    )
    db.commit()

    result = ArtifactRetentionExecutor(
        db,
        data_plane_retention_service=DataPlaneRetentionService(
            db,
            object_store=_DeleteExceptionObjectStore(base_store, fail_key=object_key),
        ),
    ).run(
        policy=_ArtifactPolicy(retention_batch_size_per_tenant=10),
        tenant_id=engagement.tenant_id,
        mode=RETENTION_RUN_MODE_APPLY,
        limit=10,
    )
    db.commit()

    assert result.counts.candidate_count == 1
    assert result.counts.applied_count == 0
    assert result.counts.failed_count == 1
    assert result.succeeded is False
    assert result.decisions[-1].outcome == RETENTION_DECISION_FAILED
    assert object_key not in str(result.to_safe_dict())
    assert base_store.head_object(object_key) is not None
