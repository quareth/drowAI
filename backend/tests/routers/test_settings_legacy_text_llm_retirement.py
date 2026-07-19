"""Regression tests for retiring legacy OpenAI text mirrors from settings."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.database import SessionLocal
from backend.models import User, UserLLMProviderCredential, UserLLMSelection
from backend.routers import settings as settings_routes


def _create_user() -> User:
    db = SessionLocal()
    try:
        user = User(
            username=f"settings-retirement-{uuid4().hex}",
            password="unused-test-password-hash",
            email=f"{uuid4().hex}@example.com",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user
    finally:
        db.close()


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


def test_settings_read_write_exclude_openai_text_llm_mirrors() -> None:
    """Settings no longer expose or persist active text LLM mirror fields."""

    user = _create_user()
    client, app = _settings_client(user)
    try:
        create_response = client.get("/api/settings/")
        assert create_response.status_code == 200, create_response.text
        assert "openai_api_key" not in create_response.json()
        assert "openai_model" not in create_response.json()
        assert "enable_ai" not in create_response.json()

        update_response = client.put(
            "/api/settings/",
            json={
                "openai_api_key": "sk-legacy-settings",
                "openai_model": "gpt-5-mini",
                "theme": "light",
            },
        )

        assert update_response.status_code == 200, update_response.text
        assert update_response.json()["theme"] == "light"
        assert "openai_api_key" not in update_response.json()
        assert "openai_model" not in update_response.json()
        assert "enable_ai" not in update_response.json()

        verify = SessionLocal()
        try:
            assert (
                verify.query(UserLLMProviderCredential)
                .filter_by(user_id=user.id)
                .count()
                == 0
            )
            assert (
                verify.query(UserLLMSelection).filter_by(user_id=user.id).count()
                == 0
            )
        finally:
            verify.close()
    finally:
        app.dependency_overrides.clear()
