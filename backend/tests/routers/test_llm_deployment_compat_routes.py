"""Tests for deployment-aware compatibility fields on legacy LLM routes."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.database import SessionLocal
from backend.models import User, UserLLMProviderCredential, UserLLMSelection
from backend.routers import llm as llm_routes
from backend.services.llm_provider import (
    LLMCredentialService,
    LLMConnectionAuthorizer,
    LLMConnectionService,
    LLMDeploymentService,
    LLMProviderSelectionService,
)
from backend.services.llm_provider.types import (
    LLMConnectionOperation,
    LLMConnectionState,
    ProviderHealthCheckResult,
)
from backend.services.llm_provider.credential_service import encrypt_api_key


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
        assert model["deployment_ref"]["deployment_id"]
        assert model["runnable"] is True
        assert selection.json()["provider"] == "openai"
        assert selection.json()["model"] == "gpt-5.2"
        assert selection.json()["deployment_ref"]["deployment_id"] == model[
            "deployment_ref"
        ]["deployment_id"]
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
