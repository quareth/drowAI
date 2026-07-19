"""Tests for connection-scoped LLM model inventory refresh."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID, ProviderModelRef
from agent.providers.llm.core.exceptions import LLMProfileNotFoundError
from agent.providers.llm.profiles.registry import require_model_profile
from backend.models import LLMCapabilityObservation, User
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.inventory_service import LLMInventoryService
from backend.services.llm_provider.operation_registry import (
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
)
from backend.services.llm_provider.types import (
    LLMConnectionAuthorizationError,
    LLMConnectionNotFoundError,
)


def test_inventory_refresh_records_connection_availability_without_catalog_mutation(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    owner, _ = identity_users
    connection = LLMConnectionService(llm_identity_db).create_draft(
        user_id=owner.id,
        display_name="HF Router",
        connection_preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id="huggingface",
    )

    before = require_model_profile(ProviderModelRef(OPENAI_PROVIDER_ID, "gpt-5.2"))
    deployments = LLMInventoryService(llm_identity_db).refresh_inventory(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=1,
        discovered_model_ids=(
            "openai/gpt-oss-20b:fireworks-ai",
            "hf/Org-Model-A",
        ),
    )

    assert tuple(deployment.wire_model_id for deployment in deployments) == (
        "openai/gpt-oss-20b:fireworks-ai",
    )
    assert all(deployment.discovery_source == "inventory" for deployment in deployments)
    assert all(deployment.availability_state == "available" for deployment in deployments)
    assert all(deployment.source_metadata == {"availability_source": "inventory_refresh"} for deployment in deployments)
    observations = llm_identity_db.execute(select(LLMCapabilityObservation)).scalars().all()
    assert observations == []
    assert require_model_profile(ProviderModelRef(OPENAI_PROVIDER_ID, "gpt-5.2")) is before
    with pytest.raises(LLMProfileNotFoundError):
        require_model_profile(
            ProviderModelRef(
                HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
                "openai/gpt-oss-20b:fireworks-ai",
            )
        )


def test_inventory_refresh_is_owner_and_revision_authorized(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    owner, other = identity_users
    connection = LLMConnectionService(llm_identity_db).create_draft(
        user_id=owner.id,
        display_name="HF Router",
        connection_preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id="huggingface",
    )
    service = LLMInventoryService(llm_identity_db)

    with pytest.raises(LLMConnectionNotFoundError):
        service.refresh_inventory(
            user_id=other.id,
            connection_id=connection.id,
            expected_connection_revision=1,
            discovered_model_ids=("foreign",),
        )
    with pytest.raises(LLMConnectionAuthorizationError):
        service.refresh_inventory(
            user_id=owner.id,
            connection_id=connection.id,
            expected_connection_revision=2,
            discovered_model_ids=("stale",),
        )
