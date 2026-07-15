"""tenant_baseline ownership-path coverage for legacy durable tables.

Responsibilities:
- Document and verify authoritative ownership joins for tenant_baseline durable surfaces
  that do not carry a direct `tenant_id` column.
- Guard semantic-memory tier boundaries so `user_profile` stays user-private and
  `task_engagement` writes require explicit tenant context.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.chat import ChatMessage
from backend.models.core import Engagement, Task
from backend.models.knowledge import KnowledgeEvidenceArchive, KnowledgeIngestionRun, KnowledgeObservation
from backend.models.provenance import ExecutionArtifact, ToolExecution
from backend.models.semantic_memory import SemanticMemory
from backend.models.core import User
from backend.models.streaming import StreamEvent
from backend.repositories.tool_execution_repository import ToolExecutionRepository
from backend.services.artifact.provenance_query_service import ArtifactProvenanceQueryService
from backend.services.knowledge.query_service import KnowledgeQueryService
from backend.services.memory.memory_models import MemoryCreateRequest, MemoryTier


def _fk_targets(model, column_name: str) -> set[str]:
    column = model.__table__.c[column_name]
    return {fk.target_fullname for fk in column.foreign_keys}


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def test_tenant_baseline_task_scoped_durable_tables_keep_ownership_path_via_tasks() -> None:
    assert _fk_targets(ToolExecution, "task_id") == {"tasks.id"}
    assert _fk_targets(ExecutionArtifact, "task_id") == {"tasks.id"}
    assert _fk_targets(StreamEvent, "task_id") == {"tasks.id"}
    assert _fk_targets(ChatMessage, "task_id") == {"tasks.id"}
    assert not Task.__table__.c["tenant_id"].nullable


def test_tenant_baseline_engagement_scoped_durable_tables_keep_ownership_path_via_engagements() -> None:
    assert _fk_targets(KnowledgeIngestionRun, "engagement_id") == {"engagements.id"}
    assert _fk_targets(KnowledgeObservation, "engagement_id") == {"engagements.id"}
    assert _fk_targets(KnowledgeEvidenceArchive, "engagement_id") == {"engagements.id"}
    assert not Engagement.__table__.c["tenant_id"].nullable


def test_tenant_baseline_semantic_memory_tier_rules_keep_user_private_and_tenant_owned_scopes() -> None:
    assert not SemanticMemory.__table__.c["user_id"].nullable
    assert SemanticMemory.__table__.c["tenant_id"].nullable
    assert SemanticMemory.__table__.c["engagement_id"].nullable
    assert SemanticMemory.__table__.c["task_id"].nullable

    user_profile = MemoryCreateRequest(
        content="profile fact",
        memory_tier=MemoryTier.USER_PROFILE,
        user_id=11,
        engagement_id=None,
        task_id=None,
    )
    assert user_profile.engagement_id is None

    with pytest.raises(ValidationError):
        MemoryCreateRequest(
            content="engagement memory",
            memory_tier=MemoryTier.TASK_ENGAGEMENT,
            user_id=11,
            tenant_id=None,
            engagement_id=22,
            task_id=99,
        )


def test_tenant_baseline_task_scoped_query_surface_blocks_cross_tenant_task_id_only_reads() -> None:
    engine, db = _build_session()
    try:
        owner = User(username="tenant-baseline-owner-task-scope", password="secret")
        db.add(owner)
        db.flush()

        task_tenant_a = Task(user_id=owner.id, tenant_id=1, name="Tenant A Task")
        task_tenant_b = Task(user_id=owner.id, tenant_id=2, name="Tenant B Task")
        db.add_all([task_tenant_a, task_tenant_b])
        db.flush()

        execution_repo = ToolExecutionRepository(db)
        query_service = ArtifactProvenanceQueryService(db)
        execution = execution_repo.create(
            task_id=task_tenant_a.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo tenant-a"},
            agent_path="langgraph",
            status="success",
            tool_call_id="tenant-baseline-task-scope-collision",
            started_at=datetime.now(timezone.utc),
        )
        execution_repo.create(
            task_id=task_tenant_b.id,
            tool_name="shell.exec",
            tool_arguments={"command": "echo tenant-b"},
            agent_path="langgraph",
            status="success",
            tool_call_id="tenant-baseline-task-scope-collision",
            started_at=datetime.now(timezone.utc),
        )
        db.commit()

        owned = query_service.get_execution_by_tool_call_id(
            task_id=task_tenant_a.id,
            tool_call_id="tenant-baseline-task-scope-collision",
            include_artifacts=False,
        )
        assert owned is not None
        assert owned["execution"]["execution_id"] == str(execution.id)

        cross_tenant = query_service.get_execution_by_id(
            execution.id,
            task_id=task_tenant_b.id,
            include_artifacts=False,
        )
        assert cross_tenant is None
    finally:
        db.close()
        engine.dispose()


def test_tenant_baseline_engagement_scoped_query_surface_filters_to_owner_boundary() -> None:
    engine, db = _build_session()
    try:
        owner = User(username="tenant-baseline-owner-eng-scope", password="secret")
        foreign = User(username="tenant-baseline-foreign-eng-scope", password="secret")
        db.add_all([owner, foreign])
        db.flush()

        owner_engagement = Engagement(user_id=owner.id, tenant_id=1, name="Owner Engagement", status="active")
        foreign_engagement = Engagement(user_id=foreign.id, tenant_id=2, name="Foreign Engagement", status="active")
        db.add_all([owner_engagement, foreign_engagement])
        db.flush()

        owner_evidence = KnowledgeEvidenceArchive(
            user_id=owner.id,
            tenant_id=owner_engagement.tenant_id,
            engagement_id=owner_engagement.id,
            task_id=None,
            source_execution_id=uuid.uuid4(),
            storage_mode="inline",
            inline_excerpt="owner evidence",
            lineage_snapshot={},
        )
        foreign_evidence = KnowledgeEvidenceArchive(
            user_id=foreign.id,
            tenant_id=foreign_engagement.tenant_id,
            engagement_id=foreign_engagement.id,
            task_id=None,
            source_execution_id=uuid.uuid4(),
            storage_mode="inline",
            inline_excerpt="foreign evidence",
            lineage_snapshot={},
        )
        db.add_all([owner_evidence, foreign_evidence])
        db.commit()

        query_service = KnowledgeQueryService(db)
        owner_page = query_service.list_evidence(user_id=owner.id)
        assert owner_page["total"] == 1
        assert owner_page["items"][0]["id"] == str(owner_evidence.id)
    finally:
        db.close()
        engine.dispose()
