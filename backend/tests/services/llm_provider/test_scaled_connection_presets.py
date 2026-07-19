"""Tests for scaled reviewed LLM connection presets on existing protocols."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from agent.providers.llm.adapters.openai.compatible_chat import (
    OPENAI_COMPATIBLE_CHAT_ADAPTER_ID,
)
from agent.providers.llm.core.capabilities import LLMCapability
from backend.models import User
from backend.services.llm_provider.connection_service import LLMConnectionService
from backend.services.llm_provider.deployment_service import LLMDeploymentService
from backend.services.llm_provider.operation_registry import (
    ANTHROPIC_BASE_URL_ENV,
    CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    GPT_OSS_20B_PROVING_PRESET_ID,
    HUGGINGFACE_BASE_URL_ENV,
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    NVIDIA_NIM_BASE_URL_ENV,
    NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
    OPENAI_BASE_URL_ENV,
    VLLM_OPENAI_COMPATIBLE_PRESET_ID,
    ConnectionOperationRegistry,
    OperationRegistryError,
)
from backend.services.llm_provider.types import (
    LLMEgressNetworkScope,
    LLMConnectionOperation,
    LLMConnectionValidationError,
)


def test_managed_preset_operator_endpoints_are_connection_scoped() -> None:
    """Each managed provider resolves only its own declarative override source."""

    registry = ConnectionOperationRegistry(
        env_getter={
            NVIDIA_NIM_BASE_URL_ENV: "http://127.0.0.1:4000/v1",
        }.get
    )

    nvidia = registry.resolve(
        LLMConnectionOperation.INFERENCE,
        provider=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    )
    huggingface = registry.resolve(
        LLMConnectionOperation.INFERENCE,
        provider=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    )
    custom = registry.resolve(
        LLMConnectionOperation.INFERENCE,
        provider=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
        base_url="https://configured.example.test/team",
    )

    assert nvidia.client_base_url == "http://127.0.0.1:4000/v1"
    assert nvidia.url == "http://127.0.0.1:4000/v1/chat/completions"
    assert nvidia.network_scope is LLMEgressNetworkScope.LOOPBACK
    assert huggingface.client_base_url == "https://router.huggingface.co/v1"
    assert custom.client_base_url == "https://configured.example.test/team/v1"


def test_native_provider_operator_endpoints_use_the_same_resolution_contract() -> None:
    """Native providers resolve explicit SDK base URLs through registry data."""

    registry = ConnectionOperationRegistry(
        env_getter={
            OPENAI_BASE_URL_ENV: "http://127.0.0.1:4100/v1",
            ANTHROPIC_BASE_URL_ENV: "http://127.0.0.1:4200",
        }.get
    )

    openai = registry.resolve(LLMConnectionOperation.INFERENCE, provider="openai")
    anthropic = registry.resolve(
        LLMConnectionOperation.INFERENCE,
        provider="anthropic",
    )

    assert openai.client_base_url == "http://127.0.0.1:4100/v1"
    assert openai.url == "http://127.0.0.1:4100/v1/chat/completions"
    assert anthropic.client_base_url == "http://127.0.0.1:4200"
    assert anthropic.url == "http://127.0.0.1:4200/v1/messages"
    assert openai.network_scope is LLMEgressNetworkScope.LOOPBACK
    assert anthropic.network_scope is LLMEgressNetworkScope.LOOPBACK


@pytest.mark.parametrize(
    "override",
    (
        "http://provider.example.test:4000",
        "http://10.0.0.1:4000",
        "http://169.254.169.254:4000",
        "ftp://127.0.0.1:4000",
        "http://user:password@127.0.0.1:4000",
        "http://127.0.0.1:4000?token=secret",
    ),
)
def test_operator_base_url_rejects_unsafe_targets(override: str) -> None:
    """Operator overrides allow public HTTPS or an exact loopback destination only."""

    registry = ConnectionOperationRegistry(
        env_getter={NVIDIA_NIM_BASE_URL_ENV: override}.get
    )

    with pytest.raises(OperationRegistryError):
        registry.resolve(
            LLMConnectionOperation.INFERENCE,
            provider=NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        )


def test_operator_public_gateway_accepts_versioned_base_url_once() -> None:
    """A public HTTPS gateway may expose the standard API below a base path."""

    registry = ConnectionOperationRegistry(
        env_getter={
            HUGGINGFACE_BASE_URL_ENV: (
                "https://gateway.example.test:8443/team/v1"
            )
        }.get
    )

    target = registry.resolve(
        LLMConnectionOperation.INFERENCE,
        provider=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    )

    assert target.url == (
        "https://gateway.example.test:8443/team/v1/chat/completions"
    )
    assert target.allowed_ports == frozenset({8443})
    assert target.network_scope is LLMEgressNetworkScope.PUBLIC


def test_scaled_presets_are_reviewed_data_on_existing_openai_compatible_protocol() -> None:
    registry = ConnectionOperationRegistry()
    expected_preset_ids = {
        GPT_OSS_20B_PROVING_PRESET_ID,
        HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
        VLLM_OPENAI_COMPATIBLE_PRESET_ID,
        CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    }

    assert registry.list_proving_preset_ids() == (GPT_OSS_20B_PROVING_PRESET_ID,)
    assert set(registry.list_connection_preset_ids()) >= expected_preset_ids
    assert registry.list_public_gpt_oss_20b_preset_ids() == (
        NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
        VLLM_OPENAI_COMPATIBLE_PRESET_ID,
    )

    presets = [
        registry.get_connection_preset(preset_id)
        for preset_id in sorted(expected_preset_ids - {GPT_OSS_20B_PROVING_PRESET_ID})
    ]
    assert {preset.adapter_id for preset in presets} == {OPENAI_COMPATIBLE_CHAT_ADAPTER_ID}
    for preset in presets:
        assert preset.auth_schema == {
            "mode": "bearer_api_key",
            "secret_fields": ("api_key",),
        }
        assert preset.runtime_family_id == "openai_compatible_chat"
        assert preset.dialect_policy_id.startswith("openai_compatible_chat.")
        assert preset.discovery_strategy == "openai_models_endpoint"
        assert preset.endpoint_policy_id in {
            "fixed_provider_v1",
            "user_https_base_url_v1",
        }
        assert preset.user_config_fields[-1] == "api_key"

    assert {
        preset.id for preset in presets if preset.fixed_base_url is not None
    } == {
        HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    }

    nvidia = registry.get_connection_preset(NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID)
    assert nvidia.canonical_model_id == "openai/gpt-oss-20b"
    assert nvidia.exact_wire_model_id == "openai/gpt-oss-20b"
    huggingface = registry.get_connection_preset(
        HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID
    )
    assert huggingface.canonical_model_id == "openai/gpt-oss-20b"
    assert huggingface.exact_wire_model_id == "openai/gpt-oss-20b:fireworks-ai"
    assert {
        preset.id for preset in presets if preset.endpoint_config_field == "base_url"
    } == {
        OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
        VLLM_OPENAI_COMPATIBLE_PRESET_ID,
        CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    }
    custom = registry.get_connection_preset(CUSTOM_OPENAI_COMPATIBLE_PRESET_ID)
    assert custom.dialect_policy_id == "openai_compatible_chat.conservative_v1"
    for preset_id in (
        GPT_OSS_20B_PROVING_PRESET_ID,
        HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
        OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
        VLLM_OPENAI_COMPATIBLE_PRESET_ID,
    ):
        preset = registry.get_connection_preset(preset_id)
        assert preset.dialect_policy_id == "openai_compatible_chat.agent_v1"
        assert {
            LLMCapability.TOOLS,
            LLMCapability.STRUCTURED_OUTPUT_NATIVE,
            LLMCapability.STREAMING_USAGE_REPORTING,
        }.issubset(preset.capability_ceiling)


def test_huggingface_preset_uses_fixed_router_endpoint_and_guarded_operation_matrix() -> None:
    registry = ConnectionOperationRegistry()
    preset = registry.get_connection_preset(HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID)

    assert preset.fixed_base_url == "https://router.huggingface.co"
    inference = registry.resolve(
        LLMConnectionOperation.INFERENCE,
        provider=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    )
    assert inference.method == "POST"
    assert inference.url == "https://router.huggingface.co/v1/chat/completions"
    assert inference.allowed_ports == frozenset({443})
    assert inference.allowed_path_prefixes == ("/v1/chat/completions",)

    inventory = registry.resolve(
        LLMConnectionOperation.INVENTORY,
        provider=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    )
    assert inventory.url == "https://router.huggingface.co/v1/models"

    with pytest.raises(OperationRegistryError):
        registry.resolve(
            LLMConnectionOperation.LIFECYCLE_CREATE,
            provider=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        )


@pytest.mark.parametrize(
    ("preset_id", "fixed_base_url", "billing_provider_id"),
    (
        (
            HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
            "https://router.huggingface.co",
            "huggingface",
        ),
        (
            NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
            "https://integrate.api.nvidia.com",
            "nvidia",
        ),
    ),
)
def test_fixed_provider_presets_use_manifest_endpoints_and_guarded_operation_matrix(
    preset_id: str,
    fixed_base_url: str,
    billing_provider_id: str,
) -> None:
    registry = ConnectionOperationRegistry()
    preset = registry.get_connection_preset(preset_id)

    assert preset.fixed_base_url == fixed_base_url
    assert preset.billing_provider_id == billing_provider_id

    for operation, method, path in (
        (LLMConnectionOperation.HEALTH, "GET", "/v1/models"),
        (LLMConnectionOperation.INVENTORY, "GET", "/v1/models"),
        (LLMConnectionOperation.CAPABILITY_PROBE, "POST", "/v1/chat/completions"),
        (LLMConnectionOperation.INFERENCE, "POST", "/v1/chat/completions"),
    ):
        target = registry.resolve(operation, provider=preset_id)
        assert target.method == method
        assert target.url == f"{fixed_base_url}{path}"
        assert target.allowed_ports == frozenset({443})
        assert target.allowed_path_prefixes == (path,)

    with pytest.raises(OperationRegistryError):
        registry.resolve(LLMConnectionOperation.LIFECYCLE_CREATE, provider=preset_id)


@pytest.mark.parametrize(
    "preset_id",
    (
        OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
        VLLM_OPENAI_COMPATIBLE_PRESET_ID,
        CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    ),
)
def test_user_endpoint_compatible_presets_require_policy_valid_https_base_url(
    preset_id: str,
) -> None:
    registry = ConnectionOperationRegistry()
    preset = registry.get_connection_preset(preset_id)

    assert preset.fixed_base_url is None
    assert preset.endpoint_config_field == "base_url"
    assert preset.endpoint_policy_id == "user_https_base_url_v1"

    with pytest.raises(OperationRegistryError):
        registry.resolve(
            LLMConnectionOperation.INFERENCE,
            provider=preset_id,
        )

    inference = registry.resolve(
        LLMConnectionOperation.INFERENCE,
        provider=preset_id,
        base_url="https://llm.example.test/team/base",
    )
    assert inference.url == "https://llm.example.test/team/base/v1/chat/completions"
    assert inference.expected_host == "llm.example.test"
    assert inference.allowed_path_prefixes == ("/team/base/v1/chat/completions",)

    for unsafe in (
        "http://llm.example.test",
        "https://user:pass@llm.example.test",
        "https://llm.example.test?target=other",
        "https://llm.example.test/%2e%2e/private",
    ):
        with pytest.raises(OperationRegistryError):
            registry.resolve(
                LLMConnectionOperation.INFERENCE,
                provider=preset_id,
                base_url=unsafe,
            )


def test_scaled_preset_drafts_validate_runtime_family_auth_and_endpoint_policy(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    owner, _ = identity_users
    service = LLMConnectionService(llm_identity_db)

    huggingface = service.create_draft(
        user_id=owner.id,
        display_name="HF Router",
        connection_preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id="huggingface",
    )
    assert huggingface.endpoint_url is None
    assert huggingface.endpoint_policy_id == "fixed_provider_v1"
    assert huggingface.non_secret_config == {"auth_mode": "bearer"}

    custom = service.create_draft(
        user_id=owner.id,
        display_name="Team vLLM",
        connection_preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id="organization_managed",
        non_secret_config={
            "base_url": "https://llm.example.test/team/base",
            "auth_mode": "bearer",
        },
    )
    assert custom.endpoint_url == "https://llm.example.test/team/base"
    assert custom.endpoint_policy_id == "user_https_base_url_v1"
    assert custom.non_secret_config == {"auth_mode": "bearer"}

    with pytest.raises(LLMConnectionValidationError):
        service.create_draft(
            user_id=owner.id,
            display_name="Unsafe",
            connection_preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            runtime_family_id="openai_native",
            serving_operator_id="organization_managed",
            non_secret_config={"base_url": "https://llm.example.test"},
        )
    with pytest.raises(LLMConnectionValidationError):
        service.create_draft(
            user_id=owner.id,
            display_name="Unsafe",
            connection_preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            runtime_family_id="openai_compatible_chat",
            serving_operator_id="organization_managed",
            non_secret_config={"base_url": "http://llm.example.test"},
        )
    with pytest.raises(LLMConnectionValidationError):
        service.create_draft(
            user_id=owner.id,
            display_name="Unsafe",
            connection_preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            runtime_family_id="openai_compatible_chat",
            serving_operator_id="organization_managed",
            non_secret_config={
                "base_url": "https://llm.example.test",
                "custom_headers": {"x-token": "not-allowed"},
            },
        )


def test_scaled_preset_deployments_reuse_existing_adapter_routes(
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

    deployment, route = LLMDeploymentService(llm_identity_db).create_preset_deployment(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=1,
        wire_model_id="openai/gpt-oss-20b:fireworks-ai",
        display_name="GPT-OSS 20B via HF",
        canonical_model_id="openai/gpt-oss-20b",
    )

    assert deployment.wire_model_id == "openai/gpt-oss-20b:fireworks-ai"
    assert deployment.canonical_model_id == "openai/gpt-oss-20b"
    assert deployment.discovery_source == "preset"
    assert deployment.source_metadata == {
        "preset_id": HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        "wire_model_source": "user_selected_preset_model",
    }
    assert route.adapter_id == OPENAI_COMPATIBLE_CHAT_ADAPTER_ID
    assert route.api_surface == "chat_completions"
    assert route.billing_provider_id == "huggingface"
    assert route.route_config == {
        "preset_id": HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
        "discovery_strategy": "openai_models_endpoint",
    }


def test_scaled_preset_deployments_normalize_gpt_oss_canonical_aliases(
    llm_identity_db: Session,
    identity_users: tuple[User, User],
) -> None:
    owner, _ = identity_users
    connection = LLMConnectionService(llm_identity_db).create_draft(
        user_id=owner.id,
        display_name="Team Custom",
        connection_preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
        runtime_family_id="openai_compatible_chat",
        serving_operator_id="organization_managed",
        non_secret_config={
            "base_url": "https://llm.example.test/team/base",
            "auth_mode": "bearer",
        },
    )

    deployment, _route = LLMDeploymentService(llm_identity_db).create_preset_deployment(
        user_id=owner.id,
        connection_id=connection.id,
        expected_connection_revision=1,
        wire_model_id="gpt-oss:20b",
        display_name="GPT-OSS 20B via Ollama",
        canonical_model_id="gpt-oss:20b",
    )

    assert deployment.wire_model_id == "gpt-oss:20b"
    assert deployment.canonical_model_id == "openai/gpt-oss-20b"
