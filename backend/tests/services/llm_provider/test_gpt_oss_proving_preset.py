"""Tests for the code-owned GPT-OSS 20B proving preset contract."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from agent.providers.llm.adapters.openai.compatible_chat import (
    CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT,
    OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
    OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION,
)
from backend.models import User
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.egress_policy import FixedProviderEgressPolicy
from backend.services.llm_provider.operation_registry import (
    GPT_OSS_20B_PROVING_API_KEY_ENV,
    GPT_OSS_20B_PROVING_BASE_URL_ENV,
    GPT_OSS_20B_PROVING_E2E_ENV,
    GPT_OSS_20B_PROVING_PRESET_ID,
    ConnectionOperationRegistry,
    OperationRegistryError,
)
from backend.services.llm_provider.types import (
    LLMConnectionOperation,
    LLMConnectionState,
    LLMConnectionValidationError,
    LLMDeploymentNotFoundError,
)


def test_gpt_oss_registry_exposes_exactly_one_fixed_proving_preset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(GPT_OSS_20B_PROVING_BASE_URL_ENV, "https://gpt-oss.example.test")

    registry = ConnectionOperationRegistry()
    preset = registry.get_proving_preset(GPT_OSS_20B_PROVING_PRESET_ID)

    assert registry.list_proving_preset_ids() == (GPT_OSS_20B_PROVING_PRESET_ID,)
    assert preset.id == GPT_OSS_20B_PROVING_PRESET_ID
    assert preset.display_name == "GPT-OSS 20B OpenAI-compatible proving"
    assert preset.canonical_model_id == "openai/gpt-oss-20b"
    assert preset.exact_wire_model_id == "openai/gpt-oss-20b"
    assert preset.adapter_id == OPENAI_COMPATIBLE_CHAT_ADAPTER_ID
    assert preset.adapter_version == OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION
    assert preset.api_surface == "chat_completions"
    assert preset.dialect_policy_id == CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT.policy_id
    assert preset.auth_mode == "bearer_api_key"
    assert preset.secret_fields == ("api_key",)
    assert preset.user_config_fields == ("display_label", "api_key")
    assert preset.base_url_env == GPT_OSS_20B_PROVING_BASE_URL_ENV
    assert preset.e2e_enabled_env == GPT_OSS_20B_PROVING_E2E_ENV
    assert preset.e2e_api_key_env == GPT_OSS_20B_PROVING_API_KEY_ENV

    inference = registry.resolve(
        LLMConnectionOperation.INFERENCE,
        provider=GPT_OSS_20B_PROVING_PRESET_ID,
    )
    assert inference.method == "POST"
    assert inference.url == "https://gpt-oss.example.test/v1/chat/completions"
    assert inference.expected_host == "gpt-oss.example.test"
    assert inference.allowed_ports == frozenset({443})
    assert inference.allowed_path_prefixes == ("/v1/chat/completions",)
    validated = FixedProviderEgressPolicy(
        dns_resolver=lambda _host, _port: ("93.184.216.34",)
    ).validate_endpoint(
        inference.url,
        expected_host=inference.expected_host,
        allowed_ports=inference.allowed_ports,
        allowed_path_prefixes=inference.allowed_path_prefixes,
    )
    assert validated.path == "/v1/chat/completions"

    inventory = registry.resolve(
        LLMConnectionOperation.INVENTORY,
        provider=GPT_OSS_20B_PROVING_PRESET_ID,
    )
    assert inventory.method == "GET"
    assert inventory.url == "https://gpt-oss.example.test/v1/models"

    with pytest.raises(OperationRegistryError):
        registry.resolve(
            LLMConnectionOperation.LIFECYCLE_CREATE,
            provider=GPT_OSS_20B_PROVING_PRESET_ID,
        )


@pytest.mark.parametrize(
    "base_url",
    (
        "http://gpt-oss.example.test",
        "https://user:pass@gpt-oss.example.test",
        "https://gpt-oss.example.test?target=other",
        "https://gpt-oss.example.test#fragment",
    ),
)
def test_gpt_oss_registry_rejects_unsafe_or_user_shaped_endpoint_env(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    monkeypatch.setenv(GPT_OSS_20B_PROVING_BASE_URL_ENV, base_url)

    with pytest.raises(OperationRegistryError):
        ConnectionOperationRegistry().resolve(
            LLMConnectionOperation.INFERENCE,
            provider=GPT_OSS_20B_PROVING_PRESET_ID,
        )


def test_gpt_oss_connection_draft_is_owner_scoped_and_singleton_per_user(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    monkeypatch.setenv(GPT_OSS_20B_PROVING_BASE_URL_ENV, "https://gpt-oss.example.test")
    owner, other = identity_users
    service = LLMConnectionService(llm_identity_db)

    connection = service.create_gpt_oss_20b_proving_draft(
        user_id=owner.id,
        display_label="Team proof",
    )

    assert connection.user_id == owner.id
    assert connection.display_name == "Team proof"
    assert connection.connection_preset_id == GPT_OSS_20B_PROVING_PRESET_ID
    assert connection.runtime_family_id == "openai_compatible_chat"
    assert connection.serving_operator_id == "openai_compatible_proving"
    assert connection.endpoint_url is None
    assert connection.non_secret_config is None
    assert connection.state == LLMConnectionState.DRAFT.value

    with pytest.raises(LLMConnectionValidationError):
        service.create_gpt_oss_20b_proving_draft(user_id=owner.id)

    other_connection = service.create_gpt_oss_20b_proving_draft(user_id=other.id)
    assert other_connection.user_id == other.id

    service.transition_state(
        user_id=owner.id,
        connection_id=connection.id,
        expected_revision=1,
        target_state=LLMConnectionState.DISABLED,
    )
    replacement = service.create_gpt_oss_20b_proving_draft(user_id=owner.id)
    assert replacement.id != connection.id


def test_generic_connection_creation_cannot_override_gpt_oss_preset_contract(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    monkeypatch.setenv(GPT_OSS_20B_PROVING_BASE_URL_ENV, "https://gpt-oss.example.test")
    owner, _ = identity_users
    service = LLMConnectionService(llm_identity_db)

    with pytest.raises(LLMConnectionValidationError):
        service.create_draft(
            user_id=owner.id,
            display_name="Unsafe",
            connection_preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
            runtime_family_id="custom_runtime",
        )
    with pytest.raises(LLMConnectionValidationError):
        service.create_draft(
            user_id=owner.id,
            display_name="Unsafe",
            connection_preset_id=GPT_OSS_20B_PROVING_PRESET_ID,
            runtime_family_id="openai_compatible_chat",
            non_secret_config={"endpoint_url": "https://user.example.test"},
        )


def test_gpt_oss_deployment_and_route_preserve_exact_code_owned_alias(
    monkeypatch: pytest.MonkeyPatch,
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    monkeypatch.setenv(GPT_OSS_20B_PROVING_BASE_URL_ENV, "https://gpt-oss.example.test")
    owner, other = identity_users
    connection = LLMConnectionService(llm_identity_db).create_gpt_oss_20b_proving_draft(
        user_id=owner.id,
    )

    deployment, route = LLMDeploymentService(
        llm_identity_db
    ).create_gpt_oss_20b_proving_deployment(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=1,
    )

    assert deployment.wire_model_id == "openai/gpt-oss-20b"
    assert deployment.canonical_model_id == "openai/gpt-oss-20b"
    assert deployment.display_name == "GPT-OSS 20B"
    assert deployment.discovery_source == "preset"
    assert deployment.source_metadata == {
        "preset_id": GPT_OSS_20B_PROVING_PRESET_ID,
        "wire_model_source": "code_owned_preset",
    }
    assert route.deployment_id == deployment.id
    assert route.adapter_id == OPENAI_COMPATIBLE_CHAT_ADAPTER_ID
    assert route.adapter_version == OPENAI_COMPATIBLE_CHAT_ADAPTER_VERSION
    assert route.api_surface == "chat_completions"
    assert route.dialect_policy_id == CONSERVATIVE_OPENAI_COMPATIBLE_DIALECT.policy_id
    assert route.billing_provider_id is None
    assert route.route_config == {"preset_id": GPT_OSS_20B_PROVING_PRESET_ID}

    with pytest.raises(LLMDeploymentNotFoundError):
        LLMDeploymentService(llm_identity_db).create_gpt_oss_20b_proving_deployment(
            user_id=other.id,
            connection_id=connection.id,
            expected_connection_revision=1,
        )
