"""Tests for tenant-scoped usage-accounting retention executor behavior."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.core.time_utils import utc_now
from backend.database import Base
from backend.models.core import Task, User
from backend.models.llm import LLMUsageRecord
from backend.models.tenant import Tenant
from backend.services.retention.contracts import (
    RETENTION_CLASS_USAGE_ACCOUNTING,
    RETENTION_DECISION_APPLIED,
    RETENTION_DECISION_CANDIDATE,
    RETENTION_RUN_MODE_APPLY,
    RETENTION_RUN_MODE_DRY_RUN,
)
from backend.services.usage_tracking.retention_service import (
    USAGE_RECORD_METADATA_RETENTION_EXPIRED,
    UsageRetentionExecutor,
)


@dataclass(frozen=True, slots=True)
class _Policy:
    usage_record_retention_days: int = 30
    retention_batch_size_per_tenant: int = 100


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def test_usage_retention_dry_run_is_tenant_scoped_and_does_not_mutate() -> None:
    db = _build_session()
    try:
        tenant, user, task = _seed_scope(db, label="dry-run")
        other_tenant, other_user, other_task = _seed_scope(db, label="other")
        old_usage = _seed_usage_record(
            db,
            tenant=tenant,
            user=user,
            task=task,
            age_days=45,
            request_metadata={"prompt": "secret dry-run prompt", "role": "planner"},
        )
        recent_usage = _seed_usage_record(
            db,
            tenant=tenant,
            user=user,
            task=task,
            age_days=5,
            request_metadata={"prompt": "recent prompt"},
        )
        already_scrubbed_usage = _seed_usage_record(
            db,
            tenant=tenant,
            user=user,
            task=task,
            age_days=45,
            request_metadata=None,
        )
        foreign_usage = _seed_usage_record(
            db,
            tenant=other_tenant,
            user=other_user,
            task=other_task,
            age_days=45,
            request_metadata={"prompt": "foreign prompt"},
        )

        result = UsageRetentionExecutor(db).run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_DRY_RUN,
            limit=100,
        )

        assert result.retention_class == RETENTION_CLASS_USAGE_ACCOUNTING
        assert result.counts.candidate_count == 1
        assert result.counts.applied_count == 0
        assert result.reason_counts == {USAGE_RECORD_METADATA_RETENTION_EXPIRED: 1}
        assert {
            (decision.outcome, decision.resource_id, decision.reason_code)
            for decision in result.decisions
        } == {
            (
                RETENTION_DECISION_CANDIDATE,
                f"llm_usage_record:{old_usage.id}",
                USAGE_RECORD_METADATA_RETENTION_EXPIRED,
            )
        }
        assert db.get(LLMUsageRecord, old_usage.id).request_metadata == {
            "prompt": "secret dry-run prompt",
            "role": "planner",
        }
        assert db.get(LLMUsageRecord, recent_usage.id).request_metadata == {
            "prompt": "recent prompt"
        }
        assert db.get(LLMUsageRecord, already_scrubbed_usage.id).request_metadata is None
        assert db.get(LLMUsageRecord, foreign_usage.id).request_metadata == {
            "prompt": "foreign prompt"
        }
    finally:
        db.close()


def test_usage_retention_apply_scrubs_metadata_and_preserves_accounting_fields() -> None:
    db = _build_session()
    try:
        tenant, user, task = _seed_scope(db, label="apply")
        other_tenant, other_user, other_task = _seed_scope(db, label="apply-other")
        old_usage = _seed_usage_record(
            db,
            tenant=tenant,
            user=user,
            task=task,
            age_days=45,
            request_metadata={
                "prompt": "sensitive prompt-like request",
                "messages": ["do not expose"],
                "role": "planner",
            },
        )
        foreign_usage = _seed_usage_record(
            db,
            tenant=other_tenant,
            user=other_user,
            task=other_task,
            age_days=45,
            request_metadata={"prompt": "foreign sensitive prompt"},
        )

        result = UsageRetentionExecutor(db).run(
            policy=_Policy(),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert result.counts.candidate_count == 1
        assert result.counts.applied_count == 1
        assert result.reason_counts == {USAGE_RECORD_METADATA_RETENTION_EXPIRED: 1}
        assert {
            (decision.outcome, decision.resource_id)
            for decision in result.decisions
        } == {
            (RETENTION_DECISION_APPLIED, f"llm_usage_record:{old_usage.id}"),
        }

        db.refresh(old_usage)
        assert old_usage.request_metadata is None
        assert old_usage.prompt_tokens == 11
        assert old_usage.completion_tokens == 13
        assert old_usage.total_tokens == 24
        assert old_usage.cached_tokens == 3
        assert old_usage.reasoning_tokens == 5
        assert old_usage.model == "gpt-5.2"
        assert old_usage.provider == "openai"
        assert old_usage.source == "langgraph_normal"
        assert _row_exists(db, old_usage.id) is True
        assert db.get(LLMUsageRecord, foreign_usage.id).request_metadata == {
            "prompt": "foreign sensitive prompt"
        }

        safe_result = str(result.to_safe_dict())
        assert "sensitive prompt-like request" not in safe_result
        assert "messages" not in safe_result
        assert "request_metadata" not in safe_result
    finally:
        db.close()


def test_usage_retention_honors_policy_and_request_batch_limits() -> None:
    db = _build_session()
    try:
        tenant, user, task = _seed_scope(db, label="batch")
        usage_records = [
            _seed_usage_record(
                db,
                tenant=tenant,
                user=user,
                task=task,
                age_days=45 + index,
                request_metadata={"role": f"planner-{index}"},
            )
            for index in range(3)
        ]

        result = UsageRetentionExecutor(db).run(
            policy=_Policy(retention_batch_size_per_tenant=2),
            tenant_id=tenant.id,
            mode=RETENTION_RUN_MODE_APPLY,
            limit=100,
        )

        assert result.counts.candidate_count == 2
        assert result.counts.batch_count == 2
        assert result.counts.batch_limit == 2
        assert result.counts.applied_count == 2
        scrubbed_ids = {
            int(decision.resource_id.rsplit(":", 1)[1])
            for decision in result.decisions
            if decision.outcome == RETENTION_DECISION_APPLIED
        }
        assert scrubbed_ids == {usage_records[2].id, usage_records[1].id}
        assert db.get(LLMUsageRecord, usage_records[0].id).request_metadata == {
            "role": "planner-0"
        }
    finally:
        db.close()


def _seed_scope(db: Session, *, label: str) -> tuple[Tenant, User, Task]:
    tenant = Tenant(slug=f"usage-retention-{label}", name=f"Usage Retention {label}")
    user = User(
        username=f"usage-retention-{label}",
        email=f"usage-retention-{label}@example.test",
        password="not-used",
    )
    db.add_all([tenant, user])
    db.flush()
    task = Task(
        tenant_id=tenant.id,
        user_id=user.id,
        name=f"Usage Retention Task {label}",
    )
    db.add(task)
    db.flush()
    return tenant, user, task


def _seed_usage_record(
    db: Session,
    *,
    tenant: Tenant,
    user: User,
    task: Task,
    age_days: int,
    request_metadata: dict[str, object] | None,
) -> LLMUsageRecord:
    record = LLMUsageRecord(
        tenant_id=tenant.id,
        user_id=user.id,
        task_id=task.id,
        prompt_tokens=11,
        completion_tokens=13,
        total_tokens=24,
        cached_tokens=3,
        reasoning_tokens=5,
        model="gpt-5.2",
        provider="openai",
        source="langgraph_normal",
        conversation_id=f"conversation-{task.id}",
        created_at=utc_now() - timedelta(days=age_days),
        request_metadata=request_metadata,
    )
    db.add(record)
    db.flush()
    return record


def _row_exists(db: Session, usage_record_id: int) -> bool:
    return db.get(LLMUsageRecord, usage_record_id) is not None
