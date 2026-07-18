"""Tests for the OpenAI settings facade over deployment-aware authorities."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.database import SessionLocal
from backend.models import (
    LLMInferenceConnection,
    User,
    UserLLMProviderCredential,
    UserLLMSelection,
    UserSettings,
)
from backend.routers import settings as settings_routes
from backend.services.llm_provider import LLMConnectionAuthorizer
from backend.services.llm_provider.credential_service import encrypt_api_key
from backend.services.llm_provider.types import (
    LLMConnectionOperation,
    ProviderHealthCheckResult,
)


def _settings_client(user: User) -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.include_router(settings_routes.router)

    def current_user() -> User:
        return user

    def db_dependency() -> Iterator:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[settings_routes.get_current_user] = current_user
    app.dependency_overrides[settings_routes.get_db] = db_dependency
    return TestClient(app), app


def test_settings_read_backfills_designated_openai_connection_and_selection() -> None:
    """Pre-Phase-2 settings remain the OpenAI credential/model facade."""

    db = SessionLocal()
    try:
        user = User(username=f"settings-facade-{uuid4().hex}", password="hashed")
        db.add(user)
        db.flush()
        db.add(
            UserSettings(
                user_id=user.id,
                openai_api_key=encrypt_api_key("sk-legacy-settings"),
                openai_model="gpt-5-mini",
                enable_ai=True,
            )
        )
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()

    client, app = _settings_client(user)
    try:
        response = client.get("/api/settings/")

        assert response.status_code == 200, response.text
        assert response.json()["openai_api_key"] == "***"
        assert response.json()["openai_model"] == "gpt-5-mini"

        verify = SessionLocal()
        try:
            credential = verify.query(UserLLMProviderCredential).filter_by(
                user_id=user.id,
                provider="openai",
            ).one()
            connection = verify.query(LLMInferenceConnection).filter_by(
                user_id=user.id,
                legacy_default_provider="openai",
            ).one()
            selection = verify.query(UserLLMSelection).filter_by(user_id=user.id).one()
            assert credential.encrypted_api_key
            assert connection.state == "enabled"
            assert selection.provider == "openai"
            assert selection.model == "gpt-5-mini"
            assert selection.deployment_id is not None
        finally:
            verify.close()
    finally:
        app.dependency_overrides.clear()


def test_settings_test_openai_authorizes_default_connection_health_operation(
    monkeypatch,
) -> None:
    """Stored-key settings tests use the guarded connection operation boundary."""

    db = SessionLocal()
    try:
        user = User(username=f"settings-test-guard-{uuid4().hex}", password="hashed")
        db.add(user)
        db.commit()
        db.refresh(user)
        from backend.services.llm_provider import LLMCredentialService

        LLMCredentialService(db).upsert_api_key(
            user_id=user.id,
            provider="openai",
            api_key="sk-guarded",
        )
        db.commit()
        db.refresh(user)
        db.expunge(user)
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
    client, app = _settings_client(user)
    try:
        response = client.post("/api/settings/test-openai", json={})

        assert response.status_code == 200, response.text
        assert len(authorizations) == 1
        assert authorizations[0]["operation"] == LLMConnectionOperation.HEALTH
        assert authorizations[0]["access_context"].authenticated_user_id == user.id
    finally:
        app.dependency_overrides.clear()
