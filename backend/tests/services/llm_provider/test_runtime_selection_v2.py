"""Tests for checkpoint-safe deployment-aware runtime selection contracts."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from backend.models import LLMDeploymentRoute, User
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.runtime_config_service import (
    LLMRuntimeConfigService,
)
from backend.services.llm_provider.types import (
    DeploymentRef,
    LLMCredentialRef,
    LLMDeploymentNotFoundError,
    LLMRuntimeAccessContext,
    LLMRuntimeSelection,
    LLMRuntimeSelectionV2,
    parse_llm_runtime_selection,
)


def test_runtime_selection_parser_preserves_typed_contracts() -> None:
    """Typed legacy and V2 selections pass through the shared parser unchanged."""

    legacy = LLMRuntimeSelection(
        provider="openai",
        model="gpt-5.2",
        credential_ref=LLMCredentialRef(user_id=7, provider="openai"),
    )
    deployment = LLMRuntimeSelectionV2(
        deployment_ref=DeploymentRef(str(uuid4()), 3),
    )

    assert parse_llm_runtime_selection(legacy) is legacy
    assert parse_llm_runtime_selection(deployment) is deployment


def test_runtime_selection_parser_distinguishes_legacy_and_v2_mappings() -> None:
    """One parser owns legacy and deployment-aware mapping discrimination."""

    legacy = parse_llm_runtime_selection(
        {
            "provider": "openai",
            "model": "gpt-5.2",
            "credential_ref": {"user_id": 7, "provider": "openai"},
        }
    )
    deployment_id = str(uuid4())
    versioned = parse_llm_runtime_selection(
        {
            "schema_version": 2,
            "deployment_ref": {
                "deployment_id": deployment_id,
                "expected_revision": 4,
            },
        }
    )
    assert isinstance(legacy, LLMRuntimeSelection)
    assert isinstance(versioned, LLMRuntimeSelectionV2)
    with pytest.raises(ValueError, match="schema_version must be 2"):
        parse_llm_runtime_selection(
            {
                "deployment_ref": {
                    "deployment_id": deployment_id,
                    "expected_revision": 4,
                },
            }
        )


def test_runtime_selection_parser_rejects_non_mapping_input() -> None:
    """Invalid runtime-selection inputs fail at the shared contract boundary."""

    with pytest.raises(
        TypeError,
        match="Runtime selection requires a mapping or selection object",
    ):
        parse_llm_runtime_selection(object())  # type: ignore[arg-type]


def test_v2_selection_round_trip_contains_only_checkpoint_safe_identity() -> None:
    """Serialization excludes endpoint, auth, profile, client, and transport facts."""

    deployment_id = str(uuid4())
    route_id = str(uuid4())
    selection = LLMRuntimeSelectionV2(
        deployment_ref=DeploymentRef(
            deployment_id=deployment_id,
            expected_revision=7,
        ),
        preferred_route_id=route_id,
        reasoning_effort="high",
        legacy_provider="openai",
        legacy_model="Org/Model-Case:Exact",
    )

    payload = selection.to_dict()

    assert payload == {
        "schema_version": 2,
        "deployment_ref": {
            "deployment_id": deployment_id,
            "expected_revision": 7,
        },
        "preferred_route_id": route_id,
        "reasoning_effort": "high",
        "legacy_provider": "openai",
        "legacy_model": "Org/Model-Case:Exact",
    }
    assert LLMRuntimeSelectionV2.from_mapping(payload) == selection
    serialized = repr(payload).lower()
    for forbidden in (
        "endpoint",
        "credential",
        "secret",
        "resolved_auth",
        "effective_profile",
        "client",
        "transport",
    ):
        assert forbidden not in serialized


def test_v2_selection_rejects_live_or_unknown_payload_fields() -> None:
    """User-controlled payloads cannot smuggle resolved infrastructure facts."""

    payload = {
        "schema_version": 2,
        "deployment_ref": {
            "deployment_id": str(uuid4()),
            "expected_revision": 1,
        },
        "endpoint": "https://attacker.invalid",
    }

    with pytest.raises(ValueError, match="unsupported fields"):
        LLMRuntimeSelectionV2.from_mapping(payload)


def test_runtime_access_context_is_trusted_and_task_tenant_bound() -> None:
    """Task identity is accepted only with its tenant and a live runtime user."""

    context = LLMRuntimeAccessContext(
        runtime_user_id=7,
        task_id=11,
        tenant_id=13,
    )

    assert context.runtime_user_id == 7
    assert not hasattr(LLMRuntimeAccessContext, "from_mapping")
    with pytest.raises(ValueError):
        LLMRuntimeAccessContext(runtime_user_id=7, task_id=11)
    with pytest.raises(ValueError):
        LLMRuntimeAccessContext(runtime_user_id=True)


def test_runtime_config_builds_v2_from_owner_scoped_current_revision(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    """Backend selection construction derives revision from an owned deployment."""

    owner, other = identity_users
    connection = LLMConnectionService(llm_identity_db).create_draft(
        user_id=owner.id,
        display_name="Owner deployment",
        connection_preset_id="openai",
        runtime_family_id="openai_native",
    )
    deployment = LLMDeploymentService(llm_identity_db).create_deployment(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=1,
        wire_model_id="gpt-5.2",
        canonical_model_id="gpt-5.2",
        display_name="GPT 5.2",
        discovery_source="catalog",
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
    llm_identity_db.add(route)
    llm_identity_db.flush()

    selection = LLMRuntimeConfigService(
        llm_identity_db
    ).build_deployment_runtime_selection(
        user_id=owner.id,
        deployment_id=str(deployment.id),
        legacy_provider="openai",
        legacy_model="gpt-5.2",
    )

    assert selection.deployment_ref == DeploymentRef(str(deployment.id), 1)
    assert selection.preferred_route_id == str(route.id)
    with pytest.raises(LLMDeploymentNotFoundError):
        LLMRuntimeConfigService(
            llm_identity_db
        ).build_deployment_runtime_selection(
            user_id=other.id,
            deployment_id=str(deployment.id),
        )
