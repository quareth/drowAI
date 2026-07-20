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
    ProviderConfigurationError,
)


def _assert_parser_failure(
    service: LLMInventoryService,
    body: bytes | None,
) -> ProviderConfigurationError:
    with pytest.raises(ProviderConfigurationError) as service_error:
        service.parse_inventory_model_ids(body)  # type: ignore[arg-type]
    return service_error.value


def test_inventory_response_parser_preserves_trimmed_order_and_duplicates(
    llm_identity_db: Session,
) -> None:
    """Usable IDs retain provider order and duplicates before filtering."""

    body = (
        b'{"data": ['
        b'{"id": "  model-b  "}, null, {"id": 7}, "ignored", '
        b'{"id": "model-a"}, {"other": "field"}, {"id": "model-b"}'
        b"]}"
    )
    service = LLMInventoryService(llm_identity_db)

    actual = service.parse_inventory_model_ids(body)

    assert actual == ("model-b", "model-a", "model-b")


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b'{"data": [}',
        b"[]",
        b'{"models": []}',
        b'{"data": {}}',
        b'\xff{"data": []}',
        None,
    ],
    ids=(
        "empty-body",
        "malformed-json",
        "wrong-top-level-type",
        "missing-data",
        "wrong-data-type",
        "invalid-utf8",
        "unsupported-input-type",
    ),
)
def test_inventory_response_parser_rejects_invalid_json_and_shape(
    llm_identity_db: Session,
    body: bytes | None,
) -> None:
    """JSON, UTF-8, and required top-level shape failures stay identical."""

    error = _assert_parser_failure(
        LLMInventoryService(llm_identity_db),
        body,
    )

    assert str(error) == "Provider inventory response is invalid"


@pytest.mark.parametrize(
    "body",
    [
        b'{"data": []}',
        b'{"data": [null, {}, {"id": 3}, {"id": ""}, {"id": "   "}]}',
    ],
    ids=("empty-data", "mixed-without-usable-ids"),
)
def test_inventory_response_parser_rejects_no_usable_models(
    llm_identity_db: Session,
    body: bytes,
) -> None:
    """Empty and fully invalid inventories preserve the stable detail."""

    error = _assert_parser_failure(
        LLMInventoryService(llm_identity_db),
        body,
    )

    assert str(error) == "Provider inventory response did not include models"


def test_inventory_response_parser_error_does_not_disclose_body_secret(
    llm_identity_db: Session,
) -> None:
    """Malformed provider content remains absent from the public error text."""

    secret = "inventory-parser-secret"
    error = _assert_parser_failure(
        LLMInventoryService(llm_identity_db),
        f'{{"data": ["{secret}"'.encode(),
    )

    assert secret not in str(error)
    assert secret not in repr(error)


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
