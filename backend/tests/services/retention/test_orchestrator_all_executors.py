"""Tests for complete retention executor registration and no-op rollout paths."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Task, User
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.tenant import Tenant
from backend.schemas.retention import RetentionRunResponse
from backend.services.artifact.retention_service import (
    ARTIFACT_PROVENANCE_PROTECTED_REASON,
)
from backend.services.retention.contracts import (
    RETENTION_CLASS_EXECUTION_PROVENANCE,
    RETENTION_DECISION_PROTECTED,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
    RETENTION_SCOPE_TENANT,
    RetentionRunRequest,
)
from backend.services.retention.orchestrator import RetentionOrchestrator
from backend.services.retention.scheduling import DEFAULT_EXECUTOR_ORDER


@dataclass(frozen=True, slots=True)
class _Policy:
    retention_batch_size_per_tenant: int = 7
    artifact_metadata_retention_days_after_terminal: int = 30


class _FakeSession:
    def __init__(self) -> None:
        self.rollback_count = 0

    def rollback(self) -> None:
        self.rollback_count += 1

    def commit(self) -> None:
        raise AssertionError("dry-run should not commit")


def test_orchestrator_reports_every_registered_executor_when_omitted() -> None:
    db = _FakeSession()

    result = RetentionOrchestrator(
        db,  # type: ignore[arg-type]
        executors=(),
        policy_resolver=(
            lambda _db, _tenant_id: _Policy()  # type: ignore[arg-type, return-value]
        ),
    ).run(
        RetentionRunRequest(
            mode=RETENTION_RUN_MODE_DRY_RUN,
            scope=RETENTION_SCOPE_TENANT,
            tenant_id=1,
        )
    )

    response = RetentionRunResponse.from_run_result(result)
    assert [item.executor_name for item in response.executor_results] == [
        entry.executor_name for entry in DEFAULT_EXECUTOR_ORDER
    ]
    assert db.rollback_count == len(DEFAULT_EXECUTOR_ORDER)
    for summary in response.executor_results:
        assert summary.counts.candidate_count == 0
        assert summary.counts.protected_count == 0
        assert summary.counts.applied_count == 0
        assert summary.counts.skipped_count == 0
        assert summary.counts.failed_count == 0
        assert summary.counts.batch_limit == 7
        assert summary.succeeded is True


def test_artifact_provenance_executor_reports_protected_rows_without_mutation() -> None:
    db = _build_provenance_session()
    try:
        tenant_id, execution_id, artifact_id = _seed_provenance_scope(
            db,
            label="protected",
            age_days=45,
        )
        other_tenant_id, other_execution_id, other_artifact_id = _seed_provenance_scope(
            db,
            label="foreign",
            age_days=45,
        )
        db.commit()

        dry_run = RetentionOrchestrator(
            db,
            policy_resolver=lambda _db, _tenant_id: _Policy(),  # type: ignore[arg-type, return-value]
        ).run(
            RetentionRunRequest(
                mode=RETENTION_RUN_MODE_DRY_RUN,
                scope=RETENTION_SCOPE_TENANT,
                tenant_id=tenant_id,
                retention_classes=(RETENTION_CLASS_EXECUTION_PROVENANCE,),
            )
        )
        apply_result = RetentionOrchestrator(
            db,
            policy_resolver=lambda _db, _tenant_id: _Policy(),  # type: ignore[arg-type, return-value]
        ).run(
            RetentionRunRequest(
                mode=RETENTION_RUN_MODE_APPLY,
                scope=RETENTION_SCOPE_TENANT,
                tenant_id=tenant_id,
                retention_classes=(RETENTION_CLASS_EXECUTION_PROVENANCE,),
            )
        )

        dry_run_summary = dry_run.results[0]
        apply_summary = apply_result.results[0]
        assert dry_run_summary.executor_name == "artifact_provenance.retention"
        assert dry_run_summary.retention_class == RETENTION_CLASS_EXECUTION_PROVENANCE
        assert dry_run_summary.counts.candidate_count == 0
        assert dry_run_summary.counts.protected_count == 2
        assert dry_run_summary.counts.preserved_count == 2
        assert dry_run_summary.reason_counts == {
            ARTIFACT_PROVENANCE_PROTECTED_REASON: 2
        }
        assert {
            (decision.outcome, decision.resource_id, decision.reason_code)
            for decision in dry_run_summary.decisions
        } == {
            (
                RETENTION_DECISION_PROTECTED,
                f"tool_execution:{execution_id}",
                ARTIFACT_PROVENANCE_PROTECTED_REASON,
            ),
            (
                RETENTION_DECISION_PROTECTED,
                f"execution_artifact:{artifact_id}",
                ARTIFACT_PROVENANCE_PROTECTED_REASON,
            ),
        }
        assert apply_summary.counts == dry_run_summary.counts
        assert apply_summary.reason_counts == dry_run_summary.reason_counts
        assert db.get(ToolExecution, execution_id) is not None
        assert db.get(ExecutionArtifact, artifact_id) is not None
        assert db.get(ToolExecution, other_execution_id) is not None
        assert db.get(ExecutionArtifact, other_artifact_id) is not None
    finally:
        db.close()


def test_artifact_provenance_executor_honors_batch_limit() -> None:
    db = _build_provenance_session()
    try:
        tenant_id, _, _ = _seed_provenance_scope(db, label="batch-a", age_days=45)
        _seed_provenance_scope(db, label="batch-b", tenant_id=tenant_id, age_days=46)
        db.commit()

        result = RetentionOrchestrator(
            db,
            policy_resolver=(
                lambda _db, _tenant_id: _Policy(retention_batch_size_per_tenant=1)  # type: ignore[arg-type, return-value]
            ),
        ).run(
            RetentionRunRequest(
                mode=RETENTION_RUN_MODE_APPLY,
                scope=RETENTION_SCOPE_TENANT,
                tenant_id=tenant_id,
                retention_classes=(RETENTION_CLASS_EXECUTION_PROVENANCE,),
                limit_per_tenant=5,
            )
        )

        summary = result.results[0]
        assert summary.counts.batch_limit == 1
        assert summary.counts.batch_count == 1
        assert summary.counts.protected_count == 1
        assert summary.reason_counts == {ARTIFACT_PROVENANCE_PROTECTED_REASON: 1}
        assert db.query(ToolExecution).filter(ToolExecution.tenant_id == tenant_id).count() == 2
        assert db.query(ExecutionArtifact).filter(ExecutionArtifact.tenant_id == tenant_id).count() == 2
    finally:
        db.close()


def _build_provenance_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Task.__table__,
            ToolExecution.__table__,
            ExecutionArtifact.__table__,
        ],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def _seed_provenance_scope(
    db: Session,
    *,
    label: str,
    age_days: int,
    tenant_id: int | None = None,
) -> tuple[int, object, object]:
    if tenant_id is None:
        tenant = Tenant(slug=f"provenance-{label}-{uuid.uuid4().hex}", name=label)
        db.add(tenant)
        db.flush()
        scoped_tenant_id = int(tenant.id)
    else:
        scoped_tenant_id = int(tenant_id)

    user = User(
        username=f"provenance-{label}-{uuid.uuid4().hex}",
        password="test-password",
        email=f"provenance-{label}-{uuid.uuid4().hex}@example.com",
    )
    db.add(user)
    db.flush()

    terminal_at = datetime.now(tz=UTC) - timedelta(days=age_days)
    task = Task(
        user_id=user.id,
        tenant_id=scoped_tenant_id,
        name=f"Provenance {label}",
        status=TaskStatus.COMPLETED.value,
        completed_at=terminal_at,
    )
    db.add(task)
    db.flush()

    execution = ToolExecution(
        tenant_id=scoped_tenant_id,
        task_id=task.id,
        tool_name="shell.exec",
        tool_arguments={"command": "echo safe"},
        agent_path="runner.tool_command",
        status="succeeded",
        started_at=terminal_at,
        finished_at=terminal_at,
        created_at=terminal_at,
    )
    db.add(execution)
    db.flush()

    artifact = ExecutionArtifact(
        execution_id=execution.id,
        tenant_id=scoped_tenant_id,
        task_id=task.id,
        artifact_kind="tool_result",
        upload_status="inline",
        content_text="safe inline output",
        is_text=True,
        artifact_metadata={"kind": "test"},
        created_at=terminal_at,
    )
    db.add(artifact)
    db.flush()
    return scoped_tenant_id, execution.id, artifact.id
