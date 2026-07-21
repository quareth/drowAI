"""Tests for deployment-aware pricing status and durable quote snapshots."""

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
from backend.services.usage_tracking.models import UsageData
from backend.services.usage_tracking.pricing import usage_from_persisted_record
from backend.services.usage_tracking.service import UsageTrackingService


def test_organization_managed_deployment_pricing_is_unavailable(
    pricing_usage_db: Session,
) -> None:
    owner, task = _usage_owner(pricing_usage_db)
    _connection, deployment, route = _usage_deployment(
        pricing_usage_db,
        user_id=owner.id,
        serving_operator_id="organization_managed",
        billing_provider_id=None,
        wire_model_id="local/team-chat",
        canonical_model_id=None,
    )

    record = UsageTrackingService(pricing_usage_db).record_usage(
        task_id=task.id,
        user_id=owner.id,
        usage=UsageData(
            prompt_tokens=80,
            completion_tokens=20,
            total_tokens=100,
            provider="openai",
            model="local/team-chat",
            api_surface="chat_completions",
        ),
        source="langgraph",
        deployment_id=str(deployment.id),
        route_id=str(route.id),
    )

    assert record is not None
    pricing = record.request_metadata["pricing_quote"]
    assert pricing["status"] == "unavailable"
    assert pricing["provider"] == "organization_managed"
    assert pricing["model"] == "local/team-chat"
    assert pricing["pricing_revision"] is None
    assert pricing["cost_usd"] == 0.0

    summary = UsageTrackingService(pricing_usage_db).get_task_usage(task.id)
    assert summary.pricing_status == "unavailable"
    assert summary.total_cost_usd == 0.0
    assert summary.unpriced_providers == ["organization_managed"]
    assert summary.unpriced_models == ["organization_managed/local/team-chat"]


def test_available_pricing_persists_applied_revision_and_cost_snapshot(
    pricing_usage_db: Session,
) -> None:
    owner, task = _usage_owner(pricing_usage_db)
    _connection, deployment, route = _usage_deployment(
        pricing_usage_db,
        user_id=owner.id,
        serving_operator_id="openai",
        billing_provider_id="openai",
        wire_model_id="gpt-5.2",
        canonical_model_id="gpt-5.2",
    )

    record = UsageTrackingService(pricing_usage_db).record_usage(
        task_id=task.id,
        user_id=owner.id,
        usage=UsageData(
            prompt_tokens=1_000,
            completion_tokens=500,
            total_tokens=1_500,
            provider="openai",
            model="gpt-5.2",
            api_surface="responses",
        ),
        source="langgraph",
        deployment_id=str(deployment.id),
        route_id=str(route.id),
    )

    assert record is not None
    pricing = record.request_metadata["pricing_quote"]
    assert pricing["status"] == "available"
    assert pricing["provider"] == "openai"
    assert pricing["model"] == "gpt-5.2"
    assert pricing["pricing_revision"]
    assert pricing["cost_usd"] > 0
    assert pricing["component_costs_usd"]["output_cost_usd"] > 0

    rebuilt_usage = usage_from_persisted_record(record)
    assert rebuilt_usage.usage_attribution is not None
    assert rebuilt_usage.usage_attribution.billing_provider_id == "openai"
    summary = UsageTrackingService(pricing_usage_db).get_task_usage(task.id)
    assert summary.pricing_status == "available"
    assert summary.total_cost_usd == pricing["cost_usd"]


def _usage_owner(db: Session) -> tuple[User, Task]:
    tenant = Tenant(slug=f"pricing-{uuid4().hex}", name="Pricing Tenant")
    owner = User(username=f"pricing-owner-{uuid4().hex}", password="hashed")
    db.add_all([tenant, owner])
    db.flush()
    task = Task(user_id=owner.id, tenant_id=tenant.id, name="Pricing Task")
    db.add(task)
    db.flush()
    return owner, task


def _usage_deployment(
    db: Session,
    *,
    user_id: int,
    serving_operator_id: str,
    billing_provider_id: str | None,
    wire_model_id: str,
    canonical_model_id: str | None,
) -> tuple[LLMInferenceConnection, LLMModelDeployment, LLMDeploymentRoute]:
    connection = LLMInferenceConnection(
        id=uuid4(),
        user_id=user_id,
        display_name="Pricing Connection",
        connection_preset_id=serving_operator_id,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id=serving_operator_id,
        transport_origin="backend",
        endpoint_policy_id="fixed_provider_v1",
        state="enabled",
        revision=3,
    )
    deployment = LLMModelDeployment(
        id=uuid4(),
        connection_id=connection.id,
        wire_model_id=wire_model_id,
        canonical_model_id=canonical_model_id,
        display_name=wire_model_id,
        discovery_source="test",
        lifecycle_state="active",
        availability_state="available",
        enabled=True,
        revision=2,
    )
    route = LLMDeploymentRoute(
        id=uuid4(),
        deployment_id=deployment.id,
        adapter_id="openai_compatible_chat",
        adapter_version="1",
        api_surface="responses",
        dialect_policy_id="openai_compatible_chat.v1",
        billing_provider_id=billing_provider_id,
        enabled=True,
    )
    db.add_all([connection, deployment, route])
    db.flush()
    return connection, deployment, route


@pytest.fixture
def pricing_usage_db() -> Iterator[Session]:
    """Yield an isolated usage/deployment database for pricing tests."""

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
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
