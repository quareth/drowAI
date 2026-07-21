"""Focused catalog route characterization for the LLM router refactor.

These tests lock the current `/api/llm/models` HTTP contract before catalog
projection moves out of the router. They must not encode target service
architecture or exercise managed/proving lifecycle mutation routes.
"""

from __future__ import annotations

from uuid import UUID

from backend.database import SessionLocal
from backend.models import (
    LLMInferenceConnection,
    LLMModelDeployment,
    UserLLMProviderCredential,
)
from backend.models.llm import UserLLMSelection
from backend.tests.routers.llm_route_test_support import (
    create_client as _client,
    create_user as _user,
)
from backend.services.llm_provider import (
    LLMCatalogApplicationService,
    LLMConnectionService,
    LLMDeploymentService,
)
from backend.services.llm_provider.catalog_projection_service import (
    LLMCatalogProjectionService,
)
from backend.services.llm_provider.credential_service import encrypt_api_key
from backend.services.llm_provider.operation_registry import (
    CUSTOM_OPENAI_COMPATIBLE_PRESET_ID,
    HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
    NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
    OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
    VLLM_OPENAI_COMPATIBLE_PRESET_ID,
)


_CATALOG_SECRET = "sk-catalog-secret"


def _assert_secret_absent(
    secret: str,
    *texts: str,
    exceptions: list[BaseException] | None = None,
) -> None:
    observed_texts = list(texts)
    for exc in exceptions or []:
        observed_texts.extend([str(exc), repr(exc)])
    for text in observed_texts:
        assert secret not in text
        assert "encrypted_api_key" not in text


def _seed_legacy_selection(
    *,
    user_id: int,
    provider: str = "openai",
    model: str = "gpt-5.2",
    secret: str = _CATALOG_SECRET,
) -> None:
    db = SessionLocal()
    try:
        db.add(
            UserLLMProviderCredential(
                user_id=user_id,
                provider=provider,
                encrypted_api_key=encrypt_api_key(secret),
                enabled=True,
            )
        )
        db.add(UserLLMSelection(user_id=user_id, provider=provider, model=model))
        db.commit()
    finally:
        db.close()


def _selection_deployment_id(user_id: int) -> UUID | None:
    db = SessionLocal()
    try:
        selection = (
            db.query(UserLLMSelection)
            .filter(UserLLMSelection.user_id == user_id)
            .one()
        )
        return selection.deployment_id
    finally:
        db.close()


def _provider(payload: dict, provider_id: str) -> dict:
    return next(provider for provider in payload["providers"] if provider["id"] == provider_id)


def _model(provider: dict, model_id: str) -> dict:
    return next(model for model in provider["models"] if model["id"] == model_id)


def test_catalog_static_provider_model_order_defaults_and_proving_metadata(
    monkeypatch,
) -> None:
    """Static catalog order, default models, field casing, and proving metadata stay stable."""

    user = _user("llm-catalog-static")
    application_calls: list[int] = []
    original_list_models = LLMCatalogApplicationService.list_models

    def recording_list_models(self, *, user_id: int):
        application_calls.append(user_id)
        return original_list_models(self, user_id=user_id)

    monkeypatch.setattr(
        LLMCatalogApplicationService,
        "list_models",
        recording_list_models,
    )
    client, app = _client(user)
    try:
        response = client.get("/api/llm/models")

        assert response.status_code == 200, response.text
        body = response.json()
        assert [provider["id"] for provider in body["providers"]] == [
            "openai",
            "anthropic",
            NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
            HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
            OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
            VLLM_OPENAI_COMPATIBLE_PRESET_ID,
        ]

        openai = _provider(body, "openai")
        assert openai["defaultModel"] == "gpt-5.2"
        assert [model["id"] for model in openai["models"][:6]] == [
            "gpt-5",
            "gpt-5-mini",
            "gpt-5-nano",
            "gpt-5-pro",
            "gpt-5.1",
            "gpt-5.2",
        ]
        gpt_oss = _model(openai, "gpt-oss-20b")
        assert "canonicalModelId" in gpt_oss
        assert "exactWireModelId" in gpt_oss
        assert "deploymentRef" in gpt_oss
        assert "deployment_ref" not in gpt_oss
        assert gpt_oss["deploymentRef"] is None
        assert gpt_oss["runnable"] is False
        assert gpt_oss["proving"] == {
            "presetId": "gpt_oss_20b_openai_compatible_proving",
            "displayName": "GPT-OSS 20B OpenAI-compatible proving",
            "enabled": True,
            "authMode": "bearer_api_key",
            "userConfigFields": ["display_label", "api_key"],
            "lifecycleState": "not_created",
            "connectionRef": None,
            "deploymentRef": None,
            "verification": {
                "status": "failed",
                "code": "not_tested",
                "message": "Verification has not run.",
                "retryable": False,
                "observed_at": None,
                "expires_at": None,
                "model_present": None,
                "usage": None,
            },
            "runnability": {
                "status": "capability_unknown",
                "selectable": True,
                "runnable": False,
                "reason": "Usage evidence is required.",
            },
        }

        anthropic = _provider(body, "anthropic")
        assert anthropic["defaultModel"] == "claude-sonnet-4-6"
        assert [model["id"] for model in anthropic["models"][:3]] == [
            "claude-fable-5",
            "claude-haiku-4-5-20251001",
            "claude-opus-4-7",
        ]
        assert application_calls == [user.id]
    finally:
        app.dependency_overrides.clear()


def test_catalog_backfills_owner_scoped_refs_and_masks_credentials(caplog) -> None:
    """Catalog reads commit only the current user's deployment backfill and safe refs."""

    user = _user("llm-catalog-owner")
    foreign = _user("llm-catalog-foreign")
    _seed_legacy_selection(user_id=user.id, model="gpt-5.2")
    _seed_legacy_selection(user_id=foreign.id, model="gpt-5-mini", secret="sk-foreign")

    foreign_client, foreign_app = _client(foreign)
    try:
        foreign_response = foreign_client.get("/api/llm/models")
        assert foreign_response.status_code == 200, foreign_response.text
    finally:
        foreign_app.dependency_overrides.clear()
    foreign_deployment_id = _selection_deployment_id(foreign.id)
    assert foreign_deployment_id is not None

    client, app = _client(user)
    try:
        response = client.get("/api/llm/models")

        assert response.status_code == 200, response.text
        body = response.json()
        openai = _provider(body, "openai")
        model = _model(openai, "gpt-5.2")
        owner_deployment_id = _selection_deployment_id(user.id)
        assert owner_deployment_id is not None
        assert model["deploymentRef"] == {
            "deployment_id": str(owner_deployment_id),
            "expected_revision": 1,
        }
        assert model["runnable"] is True
        assert str(foreign_deployment_id) not in response.text
        _assert_secret_absent(_CATALOG_SECRET, response.text, caplog.text)
        _assert_secret_absent("sk-foreign", response.text, caplog.text)
        assert openai["credential"] == {
            "user_id": user.id,
            "provider": "openai",
            "enabled": True,
            "has_api_key": True,
            "masked_api_key": "***",
            "connection_ref": {
                "connection_id": openai["credential"]["connection_ref"]["connection_id"],
                "expected_revision": 1,
            },
            "auth_mode": "api_key",
        }
        assert openai["credential"]["connection_ref"]["connection_id"]
    finally:
        app.dependency_overrides.clear()


def test_catalog_reviewed_preset_order_filters_product_and_hides_custom_rows() -> None:
    """Reviewed GPT-OSS preset rows stay ordered while generic custom rows stay hidden."""

    user = _user("llm-catalog-reviewed")
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
        db.commit()
        hf_deployment_id = str(hf_deployment.id)
        custom_deployment_id = str(custom_deployment.id)
    finally:
        db.close()

    client, app = _client(user)
    try:
        response = client.get("/api/llm/models")

        assert response.status_code == 200, response.text
        body = response.json()
        assert [provider["id"] for provider in body["providers"][2:]] == [
            NVIDIA_NIM_OPENAI_COMPATIBLE_PRESET_ID,
            HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID,
            OLLAMA_OPENAI_COMPATIBLE_PRESET_ID,
            VLLM_OPENAI_COMPATIBLE_PRESET_ID,
        ]
        assert all(
            provider["id"] != CUSTOM_OPENAI_COMPATIBLE_PRESET_ID
            for provider in body["providers"]
        )
        hf = _provider(body, HUGGINGFACE_OPENAI_COMPATIBLE_PRESET_ID)
        assert [model["id"] for model in hf["models"]] == [
            "openai/gpt-oss-20b:fireworks-ai"
        ]
        assert hf["models"][0]["canonicalModelId"] == "openai/gpt-oss-20b"
        assert hf["models"][0]["deploymentRef"]["deployment_id"] == hf_deployment_id
        assert custom_deployment_id not in response.text
    finally:
        app.dependency_overrides.clear()


def test_catalog_failure_rolls_back_backfill_and_does_not_leak_secret(monkeypatch, caplog) -> None:
    """A catalog failure after backfill preserves status/detail and no partial rows."""

    user = _user("llm-catalog-rollback")
    _seed_legacy_selection(user_id=user.id, model="gpt-5.2")
    captured_exceptions: list[BaseException] = []

    def fail_projection(*_args, **_kwargs):
        exc = RuntimeError("catalog projection failed")
        captured_exceptions.append(exc)
        raise exc

    monkeypatch.setattr(
        LLMCatalogProjectionService,
        "project",
        fail_projection,
    )

    client, app = _client(user)
    try:
        response = client.get("/api/llm/models")

        assert response.status_code == 400, response.text
        assert response.json() == {"detail": "LLM catalog application failed"}
        _assert_secret_absent(
            _CATALOG_SECRET,
            response.text,
            caplog.text,
            exceptions=captured_exceptions,
        )
    finally:
        app.dependency_overrides.clear()

    db = SessionLocal()
    try:
        selection = (
            db.query(UserLLMSelection)
            .filter(UserLLMSelection.user_id == user.id)
            .one()
        )
        deployments = (
            db.query(LLMModelDeployment)
            .join(
                LLMInferenceConnection,
                LLMModelDeployment.connection_id == LLMInferenceConnection.id,
            )
            .filter(LLMInferenceConnection.user_id == user.id)
            .all()
        )
        assert selection.deployment_id is None
        assert deployments == []
    finally:
        db.close()
