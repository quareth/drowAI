"""Tests for deployment-aware compatibility fields on legacy LLM routes."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.providers.llm.core.capabilities import LLMCapability
from backend.database import SessionLocal
from backend.models import (
    LLMCapabilityObservation,
    LLMModelDeployment,
    User,
    UserLLMProviderCredential,
    UserLLMSelection,
)
from backend.routers import llm as llm_routes
from backend.services.llm_provider import (
    LLMCredentialService,
    LLMConnectionAuthorizer,
    LLMConnectionService,
    LLMDeploymentService,
    LLMInventoryService,
    LLMProviderSelectionService,
)
from backend.services.llm_provider.types import (
    GuardedHTTPResponse,
    LLMConnectionCredentialRef,
    LLMConnectionOperation,
    LLMConnectionState,
    ProviderHealthCheckResult,
)
from backend.services.llm_provider.credential_service import encrypt_api_key
from backend.services.llm_provider.operation_registry import (
    CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
)


def _user(prefix: str) -> User:
    db = SessionLocal()
    try:
        user = User(username=f"{prefix}-{uuid4().hex}", password="hashed")
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user
    finally:
        db.close()


def _client(user: User) -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.include_router(llm_routes.router)

    def current_user() -> User:
        return user

    def db_dependency() -> Iterator:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[llm_routes.get_current_user] = current_user
    app.dependency_overrides[llm_routes.get_db] = db_dependency
    return TestClient(app), app


def _deployment(*, user_id: int, model: str):
    db = SessionLocal()
    try:
        connections = LLMConnectionService(db)
        connection = connections.create_draft(
            user_id=user_id,
            display_name=f"Compat {model}",
            connection_preset_id="openai",
            runtime_family_id="openai_native",
        )
        connection = connections.transition_state(
            user_id=user_id,
            connection_id=connection.id,
            expected_revision=1,
            target_state=LLMConnectionState.DISABLED,
        )
        connections.transition_state(
            user_id=user_id,
            connection_id=connection.id,
            expected_revision=connection.revision,
            target_state=LLMConnectionState.ENABLED,
        )
        deployment = LLMDeploymentService(db).create_deployment(
            user_id=user_id,
            connection_id=connection.id,
            expected_connection_revision=3,
            wire_model_id=model,
            canonical_model_id=model,
            display_name=model,
            discovery_source="operator",
        )
        db.commit()
        return deployment.id
    finally:
        db.close()


def test_selection_and_reporting_routes_accept_deployment_refs_with_legacy_fields() -> None:
    """Deployment writes preserve provider/model responses and opaque refs."""

    user = _user("llm-deployment-facade")
    conversation_id = _deployment(user_id=user.id, model="gpt-5.2")
    reporting_id = _deployment(user_id=user.id, model="gpt-5-mini")
    client, app = _client(user)
    try:
        conversation = client.put(
            "/api/llm/selection",
            json={
                "deployment_ref": {
                    "deployment_id": str(conversation_id),
                    "expected_revision": 1,
                }
            },
        )
        reporting = client.put(
            "/api/llm/reporting-selection",
            json={
                "deployment_ref": {
                    "deployment_id": str(reporting_id),
                    "expected_revision": 1,
                },
                "reasoning_effort": "high",
            },
        )

        assert conversation.status_code == 200, conversation.text
        assert conversation.json()["provider"] == "openai"
        assert conversation.json()["model"] == "gpt-5.2"
        assert conversation.json()["deployment_ref"] == {
            "deployment_id": str(conversation_id),
            "expected_revision": 1,
        }
        assert reporting.status_code == 200, reporting.text
        assert reporting.json()["provider"] == "openai"
        assert reporting.json()["model"] == "gpt-5-mini"
        assert reporting.json()["reasoning_effort"] == "high"
        assert reporting.json()["deployment_ref"]["deployment_id"] == str(
            reporting_id
        )

        selected = client.get("/api/llm/selection").json()
        assert selected["provider"] == "openai"
        assert selected["model"] == "gpt-5.2"
        assert selected["deployment_ref"]["deployment_id"] == str(conversation_id)
        assert selected["selection_status"]["runnable"] is True
        assert "endpoint" not in str(selected).lower()
        assert "secret" not in str(selected).lower()
    finally:
        app.dependency_overrides.clear()


def test_models_and_credentials_expose_backfilled_opaque_refs_and_runnability() -> None:
    """Legacy provider/model rows gain refs without losing compatibility fields."""

    user = _user("llm-model-catalog-facade")
    db = SessionLocal()
    try:
        credentials = LLMCredentialService(db)
        credentials.upsert_api_key(
            user_id=user.id,
            provider="openai",
            api_key="sk-compat",
        )
        LLMProviderSelectionService(db, credential_service=credentials).set_selection(
            user_id=user.id,
            provider="openai",
            model="gpt-5.2",
        )
        db.commit()
    finally:
        db.close()

    client, app = _client(user)
    try:
        credential = client.get("/api/llm/providers/openai/credential")
        catalog = client.get("/api/llm/models")
        selection = client.get("/api/llm/selection")

        assert credential.status_code == 200, credential.text
        credential_body = credential.json()
        assert credential_body["provider"] == "openai"
        assert credential_body["connection_ref"]["connection_id"]
        assert credential_body["connection_ref"]["expected_revision"] == 1
        assert credential_body["auth_mode"] == "api_key"

        assert catalog.status_code == 200, catalog.text
        openai = next(item for item in catalog.json()["providers"] if item["id"] == "openai")
        model = next(item for item in openai["models"] if item["id"] == "gpt-5.2")
        assert model["deploymentRef"]["deployment_id"]
        assert "deployment_ref" not in model
        assert model["runnable"] is True
        assert selection.json()["provider"] == "openai"
        assert selection.json()["model"] == "gpt-5.2"
        assert selection.json()["deployment_ref"]["deployment_id"] == model[
            "deploymentRef"
        ]["deployment_id"]
    finally:
        app.dependency_overrides.clear()


def test_connection_catalog_keeps_hosted_setup_api_key_first_and_endpoint_advanced() -> None:
    """Hosted presets expose only API keys while self-hosted presets require endpoints."""

    user = _user("llm-hosted-setup-catalog")
    client, app = _client(user)
    try:
        response = client.get("/api/llm/models")
        assert response.status_code == 200, response.text
        providers = {item["id"]: item for item in response.json()["providers"]}

        huggingface = providers[HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID]["models"][0][
            "connection"
        ]
        nvidia = providers[NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID]["models"][0][
            "connection"
        ]
        ollama = providers[OLLAMA_OPENAI_COMPATIBLE_PRESET_ID]["models"][0][
            "connection"
        ]

        assert huggingface["configFields"] == [
            {
                "name": "api_key",
                "label": "API key",
                "fieldType": "password",
                "required": True,
                "secret": True,
            }
        ]
        assert nvidia["configFields"] == huggingface["configFields"]
        assert "base_url" not in huggingface["userConfigFields"]
        assert "base_url" not in nvidia["userConfigFields"]
        assert [field["name"] for field in ollama["configFields"]] == [
            "base_url",
            "api_key",
            "wire_model_id",
        ]
        assert ollama["configFields"][0]["label"] == "Base URL"
        serialized_hosted = str({"huggingface": huggingface, "nvidia": nvidia}).lower()
        assert "wire model" not in serialized_hosted
        assert "adapter" not in serialized_hosted
    finally:
        app.dependency_overrides.clear()


def test_provider_credential_test_authorizes_default_connection_health_operation(
    monkeypatch,
) -> None:
    """Stored provider-key tests authorize the registered connection operation."""

    user = _user("llm-provider-test-guard")
    db = SessionLocal()
    try:
        LLMCredentialService(db).upsert_api_key(
            user_id=user.id,
            provider="openai",
            api_key="sk-provider-guard",
        )
        db.commit()
    finally:
        db.close()
    authorizations: list[dict] = []
    original_authorize = LLMConnectionAuthorizer.authorize

    def recording_authorize(self, **kwargs):
        authorizations.append(dict(kwargs))
        return original_authorize(self, **kwargs)

    monkeypatch.setattr(LLMConnectionAuthorizer, "authorize", recording_authorize)
    monkeypatch.setattr(
        "backend.services.llm_provider.health_service.LLMProviderHealthService._test_openai_key",
        lambda _self, _key: ProviderHealthCheckResult(
            provider="openai",
            status="success",
            message="OpenAI API key is valid",
            model_count=1,
        ),
    )
    client, app = _client(user)
    try:
        response = client.post(
            "/api/llm/providers/openai/credential/test",
            json={},
        )

        assert response.status_code == 200, response.text
        assert len(authorizations) == 1
        assert authorizations[0]["operation"] == LLMConnectionOperation.HEALTH
        assert authorizations[0]["access_context"].authenticated_user_id == user.id
    finally:
        app.dependency_overrides.clear()


def test_managed_connection_test_authorizes_and_uses_guarded_transport(
    monkeypatch,
) -> None:
    """Managed preset health checks authorize before guarded egress."""

    user = _user("llm-managed-preset-guard")
    db = SessionLocal()
    try:
        connection = LLMConnectionService(db).create_draft(
            user_id=user.id,
            display_name="HF Router",
            connection_preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
            runtime_family_id="openai_compatible_chat",
            serving_operator_id="huggingface",
        )
        connection_id = str(connection.id)
        db.commit()
    finally:
        db.close()

    authorizations: list[dict] = []
    original_authorize = LLMConnectionAuthorizer.authorize

    def recording_authorize(self, **kwargs):
        authorizations.append(dict(kwargs))
        return original_authorize(self, **kwargs)

    transport_calls: list[dict] = []

    class RecordingGuardedTransport:
        def execute(self, operation, **kwargs):
            transport_calls.append({"operation": operation, **kwargs})

    monkeypatch.setattr(LLMConnectionAuthorizer, "authorize", recording_authorize)
    monkeypatch.setattr(llm_routes, "GuardedTransport", RecordingGuardedTransport)

    client, app = _client(user)
    try:
        url = (
            "/api/llm/connection-presets/"
            f"{HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID}/connection/test"
        )
        response = client.post(
            url,
            json={
                "api_key": "hf-secret",
                "connection_ref": {
                    "connection_id": connection_id,
                    "expected_revision": 1,
                },
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "passed"
        assert len(authorizations) == 1
        assert authorizations[0]["operation"] == LLMConnectionOperation.HEALTH
        assert authorizations[0]["access_context"].authenticated_user_id == user.id
        assert len(transport_calls) == 1
        assert transport_calls[0]["operation"] == LLMConnectionOperation.HEALTH
        assert transport_calls[0]["provider"] == HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID
        assert transport_calls[0]["operation_target"].url == (
            "https://router.huggingface.co/v1/models"
        )
    finally:
        app.dependency_overrides.clear()


def test_managed_connection_create_registers_custom_model_through_inventory_service(
    monkeypatch,
) -> None:
    """Managed creation uses the conservative custom registration authority."""

    user = _user("llm-managed-register")
    registrations: list[dict] = []
    original_register = LLMInventoryService.register_custom_model

    def recording_register(self, **kwargs):
        registrations.append(dict(kwargs))
        return original_register(self, **kwargs)

    monkeypatch.setattr(LLMInventoryService, "register_custom_model", recording_register)
    client, app = _client(user)
    try:
        response = client.post(
            (
                "/api/llm/connection-presets/"
                f"{CUSTOM_OPENAI_COMPATIBLE_PRESET_ID}/connection"
            ),
            json={
                "api_key": "sk-team",
                "display_label": "Team endpoint",
                "base_url": "https://llm.example.test/team",
                "wire_model_id": "team/chat-model",
                "model_label": "Team Chat Model",
                "canonical_model_id": CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            },
        )

        assert response.status_code == 200, response.text
        assert len(registrations) == 1
        assert registrations[0]["user_id"] == user.id
        assert registrations[0]["wire_model_id"] == "team/chat-model"
        assert registrations[0]["requested_capabilities"] == ()
        deployment_id = response.json()["deployment_ref"]["deployment_id"]

        db = SessionLocal()
        try:
            deployment = db.get(LLMModelDeployment, UUID(deployment_id))
            assert deployment.discovery_source == "custom"
            assert deployment.canonical_model_id is None
            assert deployment.source_metadata["registration_source"] == (
                "user_custom_model"
            )
        finally:
            db.close()
    finally:
        app.dependency_overrides.clear()


def test_managed_connection_create_normalizes_explicit_gpt_oss_canonical_alias() -> None:
    """Managed creation stores the one canonical GPT-OSS identity for aliases."""

    user = _user("llm-managed-gpt-oss-normalize")
    client, app = _client(user)
    try:
        response = client.post(
            (
                "/api/llm/connection-presets/"
                f"{CUSTOM_OPENAI_COMPATIBLE_PRESET_ID}/connection"
            ),
            json={
                "api_key": "sk-team",
                "display_label": "Team GPT-OSS endpoint",
                "base_url": "https://llm.example.test/team",
                "wire_model_id": "gpt-oss:20b",
                "model_label": "Team GPT-OSS",
                "canonical_model_id": "gpt-oss:20b",
            },
        )

        assert response.status_code == 200, response.text
        deployment_id = response.json()["deployment_ref"]["deployment_id"]

        db = SessionLocal()
        try:
            deployment = db.get(LLMModelDeployment, UUID(deployment_id))
            assert deployment.canonical_model_id == "openai/gpt-oss-20b"
        finally:
            db.close()
    finally:
        app.dependency_overrides.clear()


def test_models_catalog_keeps_explicit_gpt_oss_and_generic_custom_identity_separate() -> None:
    """Catalog projection groups only explicitly canonical GPT-OSS deployments."""

    user = _user("llm-gpt-oss-canonical-grouping")
    db = SessionLocal()
    try:
        connections = LLMConnectionService(db)
        hf_connection = connections.create_draft(
            user_id=user.id,
            display_name="HF GPT-OSS",
            connection_preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
            runtime_family_id="openai_compatible_chat",
            serving_operator_id="huggingface",
            non_secret_config={"auth_mode": "bearer"},
        )
        custom_connection = connections.create_draft(
            user_id=user.id,
            display_name="Team endpoint",
            connection_preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            runtime_family_id="openai_compatible_chat",
            serving_operator_id="organization_managed",
            non_secret_config={
                "base_url": "https://llm.example.test/team",
                "auth_mode": "bearer",
            },
        )
        hf_deployment, _hf_route = LLMDeploymentService(db).create_preset_deployment(
            user_id=user.id,
            connection_id=hf_connection.id,
            expected_connection_revision=int(hf_connection.revision),
            wire_model_id="openai/gpt-oss-20b:fireworks-ai",
            display_name="GPT-OSS 20B via HF",
            canonical_model_id="openai/gpt-oss-20b",
        )
        custom_deployment, _custom_route = LLMDeploymentService(db).create_preset_deployment(
            user_id=user.id,
            connection_id=custom_connection.id,
            expected_connection_revision=int(custom_connection.revision),
            wire_model_id="team/chat-model",
            display_name="Team Chat Model",
        )
        hf_deployment_id = str(hf_deployment.id)
        custom_deployment_id = str(custom_deployment.id)
        db.commit()
    finally:
        db.close()

    client, app = _client(user)
    try:
        response = client.get("/api/llm/models")

        assert response.status_code == 200, response.text
        providers = response.json()["providers"]
        hf = next(
            provider
            for provider in providers
            if provider["id"] == HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID
        )
        custom = next(
            provider
            for provider in providers
            if provider["id"] == CUSTOM_OPENAI_COMPATIBLE_PRESET_ID
        )
        hf_model = next(
            model
            for model in hf["models"]
            if model["deploymentRef"]["deployment_id"] == hf_deployment_id
        )
        custom_model = next(
            model
            for model in custom["models"]
            if model["deploymentRef"]["deployment_id"] == custom_deployment_id
        )

        assert hf_model["canonicalModelId"] == "openai/gpt-oss-20b"
        assert hf_model["exactWireModelId"] == "openai/gpt-oss-20b:fireworks-ai"
        assert custom_model["canonicalModelId"] == "team/chat-model"
        assert custom_model["canonicalModelId"] != "openai/gpt-oss-20b"
    finally:
        app.dependency_overrides.clear()


def test_managed_connection_refresh_uses_guarded_inventory_service(
    monkeypatch,
) -> None:
    """Managed inventory refresh is owner-scoped and appears in the catalog."""

    user = _user("llm-managed-refresh")
    db = SessionLocal()
    try:
        connection = LLMConnectionService(db).create_draft(
            user_id=user.id,
            display_name="HF Router",
            connection_preset_id=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
            runtime_family_id="openai_compatible_chat",
            serving_operator_id="huggingface",
        )
        LLMCredentialService(db).upsert_connection_api_key(
            user_id=user.id,
            connection_ref=LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=int(connection.revision),
            ),
            provider=HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
            api_key="hf-secret",
        )
        db.refresh(connection)
        connection_ref = {
            "connection_id": str(connection.id),
            "expected_revision": int(connection.revision),
        }
        db.commit()
    finally:
        db.close()

    transport_calls: list[dict] = []

    class RecordingGuardedTransport:
        def execute(self, operation, **kwargs):
            transport_calls.append({"operation": operation, **kwargs})
            return GuardedHTTPResponse(
                status_code=200,
                body=b'{"data":[{"id":"hf/refreshed-a"},{"id":"hf/refreshed-b"}]}',
                audit_id="audit-refresh",
            )

    monkeypatch.setattr(llm_routes, "GuardedTransport", RecordingGuardedTransport)
    client, app = _client(user)
    try:
        response = client.post(
            (
                "/api/llm/connection-presets/"
                f"{HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID}/connection/refresh"
            ),
            json={"connection_ref": connection_ref},
        )
        catalog = client.get("/api/llm/models")

        assert response.status_code == 200, response.text
        assert len(transport_calls) == 1
        assert transport_calls[0]["operation"] == LLMConnectionOperation.INVENTORY
        assert transport_calls[0]["provider"] == HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID
        assert catalog.status_code == 200, catalog.text
        hf = next(
            item
            for item in catalog.json()["providers"]
            if item["id"] == HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID
        )
        model_ids = {model["id"] for model in hf["models"]}
        assert {"hf/refreshed-a", "hf/refreshed-b"}.issubset(model_ids)
        assert "hf-secret" not in str(response.json())
        assert "hf-secret" not in str(catalog.json())
    finally:
        app.dependency_overrides.clear()


def test_models_catalog_exposes_scaled_connection_deployments_and_schema_fields() -> None:
    """Managed catalog and selection share credential/capability runnability."""

    user = _user("llm-scaled-catalog")
    db = SessionLocal()
    try:
        connections = LLMConnectionService(db)
        connection = connections.create_draft(
            user_id=user.id,
            display_name="Team endpoint",
            connection_preset_id=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            runtime_family_id="openai_compatible_chat",
            serving_operator_id="organization_managed",
            non_secret_config={
                "base_url": "https://llm.example.test/team",
                "auth_mode": "bearer",
            },
        )
        LLMCredentialService(db).upsert_connection_api_key(
            user_id=user.id,
            connection_ref=LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=int(connection.revision),
            ),
            provider=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
            api_key="sk-team",
        )
        db.refresh(connection)
        assert connection.legacy_default_provider is None
        deployment, route = LLMDeploymentService(db).create_preset_deployment(
            user_id=user.id,
            connection_id=connection.id,
            expected_connection_revision=int(connection.revision),
            wire_model_id="team/tool-model",
            display_name="Team Tool Model",
        )
        deployment_id = str(deployment.id)
        connection = connections.transition_state(
            user_id=user.id,
            connection_id=connection.id,
            expected_revision=int(connection.revision),
            target_state=LLMConnectionState.DISABLED,
        )
        connections.transition_state(
            user_id=user.id,
            connection_id=connection.id,
            expected_revision=int(connection.revision),
            target_state=LLMConnectionState.ENABLED,
        )
        credential_fingerprint = LLMCredentialService(
            db
        ).connection_credential_fingerprint(
            user_id=user.id,
            connection_ref=LLMConnectionCredentialRef(
                connection_id=str(connection.id),
                expected_revision=int(connection.revision),
            ),
            provider=CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
        )
        connection_id = str(connection.id)
        connection_revision = int(connection.revision)
        route_id = route.id
        db.commit()
    finally:
        db.close()

    client, app = _client(user)
    try:
        catalog = client.get("/api/llm/models")

        assert catalog.status_code == 200, catalog.text
        custom = next(
            item
            for item in catalog.json()["providers"]
            if item["id"] == CUSTOM_OPENAI_COMPATIBLE_PRESET_ID
        )
        model = next(item for item in custom["models"] if item["id"] == "team/tool-model")
        assert model["deploymentRef"] == {
            "deployment_id": deployment_id,
            "expected_revision": 1,
        }
        assert "deployment_ref" not in model
        assert model["runnable"] is False
        assert model["pricingStatus"] == "unavailable"
        assert model["connection"]["presetId"] == CUSTOM_OPENAI_COMPATIBLE_PRESET_ID
        assert model["connection"]["lifecycleState"] == "enabled"
        assert model["connection"]["runnability"]["status"] == "capability_unknown"
        assert {field["name"] for field in model["connection"]["configFields"]} >= {
            "api_key",
            "base_url",
        }
        assert "endpoint_url" not in str(model).lower()
        assert "sk-team" not in str(model)

        rejected = client.put(
            "/api/llm/selection",
            json={
                "deployment_ref": {
                    "deployment_id": deployment_id,
                    "expected_revision": 1,
                }
            },
        )
        assert rejected.status_code == 400, rejected.text
        assert "Capability evidence is required" in rejected.text
    finally:
        app.dependency_overrides.clear()

    db = SessionLocal()
    try:
        db.add(
            LLMCapabilityObservation(
                id=uuid4(),
                deployment_id=UUID(deployment_id),
                route_id=route_id,
                capability=LLMCapability.CHAT.value,
                support_state="supported",
                constraints={
                    "connection_id": connection_id,
                    "connection_revision": connection_revision,
                    "credential_fingerprint": credential_fingerprint,
                },
                source="capability_probe",
                revision=1,
                fingerprint="custom-chat-supported",
            )
        )
        db.commit()
    finally:
        db.close()

    client, app = _client(user)
    try:
        catalog = client.get("/api/llm/models")
        assert catalog.status_code == 200, catalog.text
        custom = next(
            item
            for item in catalog.json()["providers"]
            if item["id"] == CUSTOM_OPENAI_COMPATIBLE_PRESET_ID
        )
        model = next(item for item in custom["models"] if item["id"] == "team/tool-model")
        assert model["runnable"] is True
        assert model["connection"]["runnability"]["status"] == "runnable"

        accepted = client.put(
            "/api/llm/selection",
            json={
                "deployment_ref": {
                    "deployment_id": deployment_id,
                    "expected_revision": 1,
                }
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["deployment_ref"] == {
            "deployment_id": deployment_id,
            "expected_revision": 1,
        }

        db = SessionLocal()
        try:
            runtime_selection = LLMProviderSelectionService(
                db
            ).build_deployment_runtime_selection(user_id=user.id)
            assert runtime_selection.deployment_ref.to_dict() == {
                "deployment_id": deployment_id,
                "expected_revision": 1,
            }
        finally:
            db.close()
    finally:
        app.dependency_overrides.clear()


def test_pre_phase2_anthropic_rows_backfill_through_legacy_routes() -> None:
    """Raw legacy Anthropic rows retain provider/model fields and gain refs."""

    user = _user("llm-anthropic-legacy-facade")
    db = SessionLocal()
    try:
        db.add_all(
            [
                UserLLMProviderCredential(
                    user_id=user.id,
                    provider="anthropic",
                    encrypted_api_key=encrypt_api_key("sk-ant-legacy"),
                    enabled=True,
                ),
                UserLLMSelection(
                    user_id=user.id,
                    provider="anthropic",
                    model="claude-sonnet-5",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    client, app = _client(user)
    try:
        credential = client.get("/api/llm/providers/anthropic/credential")
        selection = client.get("/api/llm/selection")

        assert credential.status_code == 200, credential.text
        assert credential.json()["connection_ref"]["connection_id"]
        assert selection.status_code == 200, selection.text
        assert selection.json()["provider"] == "anthropic"
        assert selection.json()["model"] == "claude-sonnet-5"
        assert selection.json()["deployment_ref"]["deployment_id"]
        assert selection.json()["selection_status"]["runnable"] is True
    finally:
        app.dependency_overrides.clear()
