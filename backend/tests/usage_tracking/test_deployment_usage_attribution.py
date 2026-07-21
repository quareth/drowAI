"""Tests for deployment-aware usage attribution contracts."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
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
from backend.services.usage_tracking.extraction import (
    UsageExtractionTarget,
    extract_usage,
)
from backend.services.usage_tracking.models import (
    UsageAttributionContext,
    UsageData,
)
from backend.services.usage_tracking.service import UsageTrackingService


def test_openai_compatible_hosts_share_parser_and_keep_target_attribution() -> None:
    response = SimpleNamespace(
        id="hf-request-1",
        model="openai/gpt-oss-20b:fireworks-ai",
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=25,
            total_tokens=125,
            prompt_tokens_details=SimpleNamespace(cached_tokens=10),
        ),
    )

    usage = extract_usage(
        response,
        UsageExtractionTarget(
            provider="huggingface",
            model="openai/gpt-oss-20b:fireworks-ai",
            api_surface="chat_completions",
            parser_provider="openai",
            attribution=UsageAttributionContext(
                connection_id=str(uuid4()),
                connection_revision=3,
                deployment_id=str(uuid4()),
                deployment_revision=2,
                route_id=str(uuid4()),
                canonical_model_id="openai/gpt-oss-20b",
                requested_model_id="openai/gpt-oss-20b:fireworks-ai",
                serving_operator_id="huggingface",
                billing_provider_id="huggingface",
                adapter_id="openai_compatible_chat",
                adapter_version="1",
                api_surface="chat_completions",
                dialect_policy_id="openai_compatible_chat.huggingface_v1",
            ),
        ),
    )

    assert usage.provider == "huggingface"
    assert usage.model == "openai/gpt-oss-20b:fireworks-ai"
    assert usage.prompt_tokens == 100
    assert usage.cached_tokens == 10
    assert usage.usage_attribution is not None
    assert usage.usage_attribution.provider_request_id == "hf-request-1"
    assert usage.usage_attribution.reported_model_id == "openai/gpt-oss-20b:fireworks-ai"
    assert usage.usage_attribution.serving_operator_id == "huggingface"
    assert usage.usage_attribution.billing_provider_id == "huggingface"
    assert usage.usage_attribution.adapter_id == "openai_compatible_chat"


def test_record_usage_persists_full_deployment_attribution(
    deployment_usage_db: Session,
) -> None:
    owner, task = _usage_owner(deployment_usage_db)
    connection, deployment, route = _usage_deployment(
        deployment_usage_db,
        user_id=owner.id,
        serving_operator_id="huggingface",
        billing_provider_id="huggingface",
    )
    usage = UsageData(
        prompt_tokens=50,
        completion_tokens=10,
        total_tokens=60,
        provider="huggingface",
        model=deployment.wire_model_id,
        api_surface="chat_completions",
        usage_attribution=UsageAttributionContext(
            reported_model_id="provider-reported-model",
            provider_request_id="provider-request-123",
            usage_completeness="actual",
        ),
    )

    record = UsageTrackingService(deployment_usage_db).record_usage(
        task_id=task.id,
        user_id=owner.id,
        usage=usage,
        source="langgraph",
        deployment_id=str(deployment.id),
        route_id=str(route.id),
    )

    assert record is not None
    attribution = record.request_metadata["usage_attribution"]
    assert attribution["connection_id"] == str(connection.id)
    assert attribution["connection_revision"] == 3
    assert attribution["deployment_id"] == str(deployment.id)
    assert attribution["deployment_revision"] == 2
    assert attribution["route_id"] == str(route.id)
    assert attribution["canonical_model_id"] == "openai/gpt-oss-20b"
    assert attribution["requested_model_id"] == deployment.wire_model_id
    assert attribution["reported_model_id"] == "provider-reported-model"
    assert attribution["serving_operator_id"] == "huggingface"
    assert attribution["billing_provider_id"] == "huggingface"
    assert attribution["adapter_id"] == "openai_compatible_chat"
    assert attribution["adapter_version"] == "1"
    assert attribution["api_surface"] == "chat_completions"
    assert attribution["dialect_policy_id"] == "openai_compatible_chat.huggingface_v1"
    assert attribution["provider_request_id"] == "provider-request-123"
    assert attribution["usage_completeness"] == "actual"


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
    serving_operator_id: str,
    billing_provider_id: str | None,
) -> tuple[LLMInferenceConnection, LLMModelDeployment, LLMDeploymentRoute]:
    connection = LLMInferenceConnection(
        id=uuid4(),
        user_id=user_id,
        display_name="Usage Connection",
        connection_preset_id="huggingface_openai_compatible",
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
        wire_model_id="openai/gpt-oss-20b:fireworks-ai",
        canonical_model_id="openai/gpt-oss-20b",
        display_name="GPT-OSS 20B",
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
        api_surface="chat_completions",
        dialect_policy_id="openai_compatible_chat.huggingface_v1",
        billing_provider_id=billing_provider_id,
        enabled=True,
    )
    db.add_all([connection, deployment, route])
    db.flush()
    return connection, deployment, route


@pytest.fixture
def deployment_usage_db() -> Iterator[Session]:
    """Yield an isolated usage/deployment database for attribution tests."""

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
