"""Tests for dual-written deployment identity on usage records."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models import (
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
    LLMUsageRecord,
    Task,
    Tenant,
    User,
)
from backend.services.langgraph_chat.runtime.usage_middleware import (
    record_usage_list_best_effort,
)
from backend.services.usage_tracking.models import UsageData
from backend.services.usage_tracking.service import UsageTrackingService


@pytest.fixture
def usage_identity_db() -> Iterator[Session]:
    """Yield an isolated database with task, usage, and deployment tables."""

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Task.__table__,
            LLMInferenceConnection.__table__,
            LLMModelDeployment.__table__,
            LLMDeploymentRoute.__table__,
            LLMUsageRecord.__table__,
        ],
    )
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _usage_owner(db: Session) -> tuple[User, Task]:
    tenant = Tenant(slug=f"usage-{uuid4().hex}", name="Usage Tenant")
    owner = User(username=f"usage-owner-{uuid4().hex}", password="hashed")
    db.add_all([tenant, owner])
    db.flush()
    task = Task(user_id=owner.id, tenant_id=tenant.id, name="Usage Task")
    db.add(task)
    db.flush()
    return owner, task


def _usage_deployment(
    db: Session,
    *,
    user_id: int,
) -> tuple[LLMInferenceConnection, LLMModelDeployment, LLMDeploymentRoute]:
    connection = LLMInferenceConnection(
        id=uuid4(),
        user_id=user_id,
        display_name="Usage OpenAI",
        connection_preset_id="openai",
        runtime_family_id="openai_native",
        serving_operator_id="openai",
        transport_origin="backend",
        endpoint_policy_id="fixed_provider_v1",
        state="enabled",
        revision=3,
    )
    deployment = LLMModelDeployment(
        id=uuid4(),
        connection_id=connection.id,
        wire_model_id="gpt-5.2",
        canonical_model_id="gpt-5.2",
        display_name="gpt-5.2",
        discovery_source="test",
        lifecycle_state="active",
        availability_state="available",
        enabled=True,
        revision=2,
    )
    route = LLMDeploymentRoute(
        id=uuid4(),
        deployment_id=deployment.id,
        adapter_id="openai_responses",
        adapter_version="1",
        api_surface="responses",
        dialect_policy_id="openai_responses.native_v1",
        enabled=True,
    )
    db.add_all([connection, deployment, route])
    db.flush()
    return connection, deployment, route


def test_record_usage_persists_deployment_refs_and_compatibility_snapshots(
    usage_identity_db: Session,
) -> None:
    """Usage rows keep deployment refs without replacing provider/model labels."""

    owner, task = _usage_owner(usage_identity_db)
    connection, deployment, route = _usage_deployment(
        usage_identity_db,
        user_id=owner.id,
    )
    usage = UsageData(
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        provider="openai",
        model="gpt-5.2",
        api_surface="responses",
    )

    record = UsageTrackingService(usage_identity_db).record_usage(
        task_id=task.id,
        user_id=owner.id,
        usage=usage,
        source="langgraph",
        conversation_id="conv-usage",
        deployment_id=str(deployment.id),
        route_id=str(route.id),
    )

    assert record is not None
    assert record.provider == "openai"
    assert record.model == "gpt-5.2"
    assert record.connection_id == connection.id
    assert record.deployment_id == deployment.id
    assert record.route_id == route.id


def test_record_usage_list_best_effort_forwards_v2_runtime_identity(
    usage_identity_db: Session,
) -> None:
    """LangGraph usage persistence lifts deployment refs from V2 selection."""

    owner, task = _usage_owner(usage_identity_db)
    connection, deployment, route = _usage_deployment(
        usage_identity_db,
        user_id=owner.id,
    )
    usage = UsageData(
        prompt_tokens=20,
        completion_tokens=10,
        total_tokens=30,
        provider="openai",
        model="gpt-5.2",
    )

    record_usage_list_best_effort(
        task_id=task.id,
        user_id=owner.id,
        usage_list=[usage],
        source="langgraph",
        conversation_id="conv-usage",
        runtime_selection={
            "schema_version": 2,
            "deployment_ref": {
                "deployment_id": str(deployment.id),
                "expected_revision": 2,
            },
            "preferred_route_id": str(route.id),
            "legacy_provider": "openai",
            "legacy_model": "gpt-5.2",
        },
        session_factory=lambda: usage_identity_db,
    )

    record = usage_identity_db.query(LLMUsageRecord).one()
    assert record.provider == "openai"
    assert record.model == "gpt-5.2"
    assert record.connection_id == connection.id
    assert record.deployment_id == deployment.id
    assert record.route_id == route.id
