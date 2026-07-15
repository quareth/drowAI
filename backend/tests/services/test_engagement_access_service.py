"""Tests for reusable tenant-scoped engagement lookup helpers."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models.core import Engagement, User
from backend.services.engagement.access_service import (
    get_engagement_in_tenant,
    get_engagement_in_tenant_or_404,
    get_owned_engagement,
)


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def _seed_user(db, username: str) -> User:
    user = User(username=username, password="secret")
    db.add(user)
    db.flush()
    return user


def test_get_engagement_in_tenant_returns_engagement() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "engagement-access-owner")
        engagement = Engagement(user_id=owner.id, tenant_id=101, name="Owned", status="active")
        db.add(engagement)
        db.commit()
        db.refresh(engagement)

        resolved = get_engagement_in_tenant(db=db, engagement_id=engagement.id, tenant_id=101)

        assert resolved is not None
        assert resolved.id == engagement.id
        assert resolved.tenant_id == 101
    finally:
        db.close()
        engine.dispose()


def test_get_engagement_in_tenant_returns_none_for_foreign_tenant() -> None:
    engine, db = _build_session()
    try:
        owner = _seed_user(db, "engagement-access-foreign-owner")
        _seed_user(db, "engagement-access-foreign-user")
        engagement = Engagement(user_id=owner.id, tenant_id=201, name="Owned", status="active")
        db.add(engagement)
        db.commit()
        db.refresh(engagement)

        assert get_engagement_in_tenant(db=db, engagement_id=engagement.id, tenant_id=202) is None

        try:
            get_engagement_in_tenant_or_404(db=db, engagement_id=engagement.id, tenant_id=202)
            assert False, "Expected HTTPException for foreign tenant engagement access"
        except HTTPException as exc:
            assert exc.status_code == 404
            assert exc.detail == "Engagement not found"
    finally:
        db.close()
        engine.dispose()


def test_get_owned_engagement_requires_explicit_tenant_scope() -> None:
    engine, db = _build_session()
    try:
        user = _seed_user(db, "engagement-access-missing")
        engagement = Engagement(user_id=user.id, tenant_id=1, name="Owned", status="active")
        db.add(engagement)
        db.commit()
        db.refresh(engagement)

        resolved = get_owned_engagement(db=db, engagement_id=engagement.id, user_id=user.id, tenant_id=1)
        assert resolved is not None
        assert resolved.id == engagement.id

        assert get_owned_engagement(db=db, engagement_id=999_999, user_id=user.id, tenant_id=1) is None
    finally:
        db.close()
        engine.dispose()
