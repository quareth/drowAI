"""Shared user and HTTP client builders for focused LLM route tests."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.database import SessionLocal
from backend.models import User
from backend.routers import llm as llm_routes


def create_user(prefix: str) -> User:
    """Persist and detach one uniquely named route-test user."""

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


def create_client(user: User) -> tuple[TestClient, FastAPI]:
    """Build an isolated LLM router client authenticated as ``user``."""

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


__all__ = ["create_client", "create_user"]
