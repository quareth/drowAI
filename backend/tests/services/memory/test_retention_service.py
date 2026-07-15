"""Tests for tenant-scoped semantic-memory retention executor behavior."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import hashlib
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.core.time_utils import utc_now
from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.semantic_memory import SemanticMemory
from backend.models.tenant import Tenant
from backend.services.memory.memory_models import MemoryTier
from backend.services.memory.retention_service import (
    ACTIVE_ENGAGEMENT_SEMANTIC_MEMORY_PROTECTED,
    STALE_SEMANTIC_MEMORY_UNUSED,
    MemoryRetentionExecutor,
)
from backend.services.retention.contracts import (
    RETENTION_CLASS_SEMANTIC_MEMORY,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_DECISION_PROTECTED,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
)


@dataclass(frozen=True, slots=True)
class _Policy:
    semantic_memory_stale_retention_days: int = 30
    retention_batch_size_per_tenant: int = 100


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def test_memory_retention_dry_run_is_tenant_scoped_and_does_not_mutate() -> None:
    db = _build_session()
    try:
        tenant, user, archived_engagement = _seed_scope(
            db,
            label="dry-run",
            engagement_status="archived",
        )
        other_tenant, other_user, other_archived_engagement = _seed_scope(
            db,
            label="other",
            engagement_status="archived",
        )
        active_engagement = _seed_engagement(
            db,
            tenant=tenant,
            user=user,
            label="active",
            status="active",
        )
        stale_memory = _seed_memory(
            db,
            tenant=tenant,
            user=user,
            engagement=archived_engagement,
            tier=MemoryTier.TASK_ENGAGEMENT,
            age_days=45,
            content="sensitive stale engagement memory",
        )
        active_memory = _seed_memory(
            db,
            tenant=tenant,
            user=user,
            engagement=active_engagement,
            tier=MemoryTier.TASK_ENGAGEMENT,
            age_days=45,
            content="sensitive active engagement memory",
        )
        recent_memory = _seed_memory(
            db,
            tenant=tenant,
            user=user,
            engagement=archived_engagement,
            tier=MemoryTier.TASK_ENGAGEMENT,
            age_days=5,
            content="recent memory",
        )
        foreign_memory = _seed_memory(
            db,
            tenant=other_tenant,
            user=other_user,
            engagement=other_archived_engagement,
            tier=MemoryTier.TASK_ENGAGEMENT,
            age_days=45,
            content="foreign memory",
        )

        result = MemoryRetentionExecutor(db).run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_DRY_RUN,
            limit=100,
        )

        assert result.retention_class == RETENTION_CLASS_SEMANTIC_MEMORY
        assert result.counts.candidate_count == 1
        assert result.counts.protected_count == 1
        assert result.counts.applied_count == 0
        assert result.reason_counts == {
            ACTIVE_ENGAGEMENT_SEMANTIC_MEMORY_PROTECTED: 1,
            STALE_SEMANTIC_MEMORY_UNUSED: 1,
        }
        assert {
            (decision.outcome, decision.resource_id, decision.reason_code)
            for decision in result.decisions
        } == {
            (
                RETENTION_DECISION_PROTECTED,
                f"semantic_memory:{active_memory.id}",
                ACTIVE_ENGAGEMENT_SEMANTIC_MEMORY_PROTECTED,
            ),
            (
                RETENTION_DECISION_CANDIDATE,
                f"semantic_memory:{stale_memory.id}",
                STALE_SEMANTIC_MEMORY_UNUSED,
            ),
        }
        assert db.get(SemanticMemory, stale_memory.id) is not None
        assert db.get(SemanticMemory, active_memory.id) is not None
        assert db.get(SemanticMemory, recent_memory.id) is not None
        assert db.get(SemanticMemory, foreign_memory.id) is not None
    finally:
        db.close()


def test_memory_retention_apply_deletes_only_stale_task_engagement_memory() -> None:
    db = _build_session()
    try:
        tenant, user, archived_engagement = _seed_scope(
            db,
            label="apply",
            engagement_status="archived",
        )
        other_tenant, other_user, other_archived_engagement = _seed_scope(
            db,
            label="apply-other",
            engagement_status="archived",
        )
        active_engagement = _seed_engagement(
            db,
            tenant=tenant,
            user=user,
            label="apply-active",
            status="active",
        )
        stale_memory = _seed_memory(
            db,
            tenant=tenant,
            user=user,
            engagement=archived_engagement,
            tier=MemoryTier.TASK_ENGAGEMENT,
            age_days=45,
            content="stale task engagement memory",
        )
        task_memory = _seed_task_scoped_memory(
            db,
            tenant=tenant,
            user=user,
            engagement=active_engagement,
            age_days=45,
            content="task scoped active engagement memory",
        )
        user_profile_memory = _seed_memory(
            db,
            tenant=None,
            user=user,
            engagement=None,
            tier=MemoryTier.USER_PROFILE,
            age_days=45,
            content="user profile memory must survive",
        )
        foreign_memory = _seed_memory(
            db,
            tenant=other_tenant,
            user=other_user,
            engagement=other_archived_engagement,
            tier=MemoryTier.TASK_ENGAGEMENT,
            age_days=45,
            content="foreign stale task engagement memory",
        )

        result = MemoryRetentionExecutor(db).run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert result.counts.candidate_count == 1
        assert result.counts.protected_count == 1
        assert result.counts.applied_count == 1
        assert result.reason_counts == {
            ACTIVE_ENGAGEMENT_SEMANTIC_MEMORY_PROTECTED: 1,
            STALE_SEMANTIC_MEMORY_UNUSED: 1,
        }
        assert {
            (decision.outcome, decision.resource_id)
            for decision in result.decisions
            if decision.outcome == RETENTION_DECISION_APPLIED
        } == {
            (RETENTION_DECISION_APPLIED, f"semantic_memory:{stale_memory.id}"),
        }
        assert _row_exists(db, stale_memory.id) is False
        assert _row_exists(db, task_memory.id) is True
        assert _row_exists(db, user_profile_memory.id) is True
        assert _row_exists(db, foreign_memory.id) is True
        safe_result = str(result.to_safe_dict())
        assert "stale task engagement memory" not in safe_result
        assert "user profile memory must survive" not in safe_result
    finally:
        db.close()


def test_memory_retention_honors_per_tenant_batch_limit() -> None:
    db = _build_session()
    try:
        tenant, user, archived_engagement = _seed_scope(
            db,
            label="batch",
            engagement_status="archived",
        )
        memories = [
            _seed_memory(
                db,
                tenant=tenant,
                user=user,
                engagement=archived_engagement,
                tier=MemoryTier.TASK_ENGAGEMENT,
                age_days=45,
                content=f"batch memory {idx}",
            )
            for idx in range(3)
        ]

        result = MemoryRetentionExecutor(db).run(
            policy=_Policy(retention_batch_size_per_tenant=2),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert result.counts.candidate_count == 2
        assert result.counts.batch_count == 2
        assert result.counts.batch_limit == 2
        assert result.counts.applied_count == 2
        assert (
            db.query(SemanticMemory)
            .filter(SemanticMemory.id.in_([memory.id for memory in memories]))
            .count()
            == 1
        )
    finally:
        db.close()


def _seed_scope(
    db: Session,
    *,
    label: str,
    engagement_status: str,
) -> tuple[Tenant, User, Engagement]:
    tenant = Tenant(slug=f"tenant-{label}", name=f"Tenant {label}")
    user = User(username=f"user-{label}", password="secret")
    db.add_all([tenant, user])
    db.flush()
    engagement = _seed_engagement(
        db,
        tenant=tenant,
        user=user,
        label=label,
        status=engagement_status,
    )
    return tenant, user, engagement


def _seed_engagement(
    db: Session,
    *,
    tenant: Tenant,
    user: User,
    label: str,
    status: str,
) -> Engagement:
    engagement = Engagement(
        tenant_id=tenant.id,
        user_id=user.id,
        name=f"Engagement {label}",
        status=status,
    )
    db.add(engagement)
    db.flush()
    return engagement


def _seed_task_scoped_memory(
    db: Session,
    *,
    tenant: Tenant,
    user: User,
    engagement: Engagement,
    age_days: int,
    content: str,
) -> SemanticMemory:
    task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        engagement_id=engagement.id,
        name=f"Task {uuid.uuid4()}",
    )
    db.add(task)
    db.flush()
    return _seed_memory(
        db,
        tenant=tenant,
        user=user,
        engagement=None,
        task=task,
        tier=MemoryTier.TASK_ENGAGEMENT,
        age_days=age_days,
        content=content,
    )


def _seed_memory(
    db: Session,
    *,
    tenant: Tenant | None,
    user: User,
    engagement: Engagement | None,
    tier: MemoryTier,
    age_days: int,
    content: str,
    task: Task | None = None,
) -> SemanticMemory:
    timestamp = utc_now() - timedelta(days=age_days)
    content_hash = hashlib.sha256(
        f"{content}-{uuid.uuid4()}".encode("utf-8")
    ).hexdigest()
    memory = SemanticMemory(
        tenant_id=tenant.id if tenant is not None else None,
        user_id=user.id,
        engagement_id=engagement.id if engagement is not None else None,
        task_id=task.id if task is not None else None,
        memory_tier=tier.value,
        content=content,
        scope_key=f"scope:{tier.value}:{content_hash}",
        content_hash=content_hash,
        embedding=[0.1] * 1536,
        source_type="chat_extraction",
        last_accessed_at=timestamp,
        created_at=timestamp,
        updated_at=timestamp,
    )
    db.add(memory)
    db.flush()
    return memory


def _row_exists(db: Session, memory_id: object) -> bool:
    return bool(
        db.query(SemanticMemory.id)
        .filter(SemanticMemory.id == memory_id)
        .first()
    )
