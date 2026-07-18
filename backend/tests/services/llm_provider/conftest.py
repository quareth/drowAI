"""Shared isolated database fixtures for deployment identity service tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.models import (
    LLMDeploymentRoute,
    LLMInferenceConnection,
    LLMModelDeployment,
    Task,
    Tenant,
    User,
    UserLLMProviderCredential,
    UserLLMSelection,
    UserMemoryLLMSelection,
    UserReportingLLMSelection,
    UserSettings,
)


@pytest.fixture
def llm_identity_db() -> Iterator[Session]:
    """Yield an isolated session containing deployment identity tables."""

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            UserSettings.__table__,
            Task.__table__,
            UserLLMProviderCredential.__table__,
            UserLLMSelection.__table__,
            UserReportingLLMSelection.__table__,
            UserMemoryLLMSelection.__table__,
            LLMInferenceConnection.__table__,
            LLMModelDeployment.__table__,
            LLMDeploymentRoute.__table__,
        ],
    )
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        expire_on_commit=False,
    )
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture
def identity_users(llm_identity_db: Session) -> tuple[User, User]:
    """Create two users for ownership and isolation checks."""

    owner = User(username="llm-identity-owner", password="hashed")
    other = User(username="llm-identity-other", password="hashed")
    llm_identity_db.add_all([owner, other])
    llm_identity_db.flush()
    return owner, other
