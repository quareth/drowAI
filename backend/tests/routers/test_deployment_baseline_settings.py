"""Deployment baseline tests for legacy settings LLM compatibility routes."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.providers.llm.core.identity import OPENAI_PROVIDER_ID
from backend.database import SessionLocal
from backend.models import User, UserLLMProviderCredential, UserLLMSelection
from backend.routers import settings as settings_routes


def _create_user(db, username_prefix: str = "deployment-settings") -> User:
    user = User(
        username=f"{username_prefix}-{uuid4().hex}",
        password="unused-test-password-hash",
        email=f"{uuid4().hex}@example.com",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _build_settings_client(user: User) -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.include_router(settings_routes.router)

    def fake_current_user() -> User:
        return user

    def fake_db() -> Iterator:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[settings_routes.get_current_user] = fake_current_user
    app.dependency_overrides[settings_routes.get_db] = fake_db
    return TestClient(app), app


def test_settings_openai_model_write_updates_provider_selection_without_key() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db)
    finally:
        db.close()

    client, app = _build_settings_client(user)
    try:
        create_response = client.get("/api/settings/")
        assert create_response.status_code == 200, create_response.text

        response = client.put("/api/settings/", json={"openai_model": "GPT-5-MINI"})
        assert response.status_code == 200, response.text
        assert response.json()["openai_model"] == "gpt-5-mini"
        assert response.json()["openai_api_key"] is None

        read_response = client.get("/api/settings/")
        assert read_response.status_code == 200, read_response.text
        assert read_response.json()["openai_model"] == "gpt-5-mini"

        verify_db = SessionLocal()
        try:
            selection = verify_db.query(UserLLMSelection).filter_by(
                user_id=user.id,
            ).one()
            assert selection.provider == OPENAI_PROVIDER_ID
            assert selection.model == "gpt-5-mini"
        finally:
            verify_db.close()
    finally:
        app.dependency_overrides.clear()


def test_settings_openai_api_key_write_uses_provider_credential_mirror() -> None:
    db = SessionLocal()
    try:
        user = _create_user(db, "deployment-settings-key")
    finally:
        db.close()

    client, app = _build_settings_client(user)
    try:
        create_response = client.get("/api/settings/")
        assert create_response.status_code == 200, create_response.text

        response = client.put(
            "/api/settings/",
            json={"openai_api_key": "sk-settings", "openai_model": "gpt-5.2"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["openai_api_key"] == "***"
        assert response.json()["openai_model"] == "gpt-5.2"

        verify_db = SessionLocal()
        try:
            credential = verify_db.query(UserLLMProviderCredential).filter_by(
                user_id=user.id,
                provider=OPENAI_PROVIDER_ID,
            ).one()
            assert credential.enabled is True
            assert credential.has_api_key is True
            assert credential.encrypted_api_key != "sk-settings"
        finally:
            verify_db.close()
    finally:
        app.dependency_overrides.clear()
