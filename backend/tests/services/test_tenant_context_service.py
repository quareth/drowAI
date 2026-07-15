"""Tests for tenant context resolution and default-tenant bootstrap behavior."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.models import Tenant, TenantMembership, User
from backend.services.tenant.bootstrap import bootstrap_default_tenant_state
from backend.services.tenant.context import (
    DEFAULT_TENANT_ID,
    TenantContextResolutionError,
    TenantContextService,
)


def _build_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return engine, session_factory()


def test_resolve_for_user_uses_single_membership_without_explicit_tenant() -> None:
    engine, db = _build_session()
    try:
        user = User(username="tenant-existing-user", password="secret")
        db.add(user)
        db.flush()

        tenant = Tenant(id=7, slug="tenant-seven", name="Tenant Seven")
        membership = TenantMembership(tenant_id=7, user_id=user.id, role="owner")
        db.add_all([tenant, membership])
        db.commit()

        resolved = TenantContextService(db).resolve_for_user(user_id=user.id)

        assert resolved is not None
        assert resolved.tenant_id == 7
        assert resolved.user_id == user.id
        assert resolved.role == "owner"
        assert resolved.source == "single_membership"
    finally:
        db.close()
        engine.dispose()


def test_resolve_for_user_rejects_user_without_membership() -> None:
    engine, db = _build_session()
    try:
        user = User(username="tenant-repair-user", password="secret")
        db.add(user)
        db.commit()
        db.refresh(user)

        with pytest.raises(TenantContextResolutionError) as exc_info:
            TenantContextService(db).resolve_for_user(user_id=user.id)

        assert exc_info.value.code == "no_active_membership"
        default_tenant = db.execute(
            select(Tenant).where(Tenant.id == DEFAULT_TENANT_ID)
        ).scalar_one_or_none()
        assert default_tenant is None

        membership = db.execute(
            select(TenantMembership).where(
                TenantMembership.tenant_id == DEFAULT_TENANT_ID,
                TenantMembership.user_id == user.id,
            )
        ).scalar_one_or_none()
        assert membership is None
    finally:
        db.close()
        engine.dispose()


def test_standalone_repair_does_not_auto_enroll_when_non_default_tenant_exists() -> None:
    engine, db = _build_session()
    try:
        user = User(username="tenant-no-membership-user", password="secret")
        other_tenant = Tenant(id=2, slug="tenant-two", name="Tenant Two")
        db.add_all([user, other_tenant])
        db.commit()
        db.refresh(user)

        service = TenantContextService(db)
        memberships = service.list_membership_summaries_for_user(user_id=user.id)
        assert memberships == []

        with pytest.raises(TenantContextResolutionError) as exc_info:
            service.resolve_for_user(user_id=user.id, allow_ambiguous=True)
        assert exc_info.value.code == "no_active_membership"
    finally:
        db.close()
        engine.dispose()


def test_resolve_for_user_requires_explicit_selection_for_multiple_memberships() -> None:
    engine, db = _build_session()
    try:
        user = User(username="multi-membership-user", password="secret")
        tenant_one = Tenant(id=11, slug="tenant-11", name="Tenant 11")
        tenant_two = Tenant(id=12, slug="tenant-12", name="Tenant 12")
        db.add_all([user, tenant_one, tenant_two])
        db.flush()
        db.add_all(
            [
                TenantMembership(tenant_id=11, user_id=user.id, role="owner"),
                TenantMembership(tenant_id=12, user_id=user.id, role="viewer"),
            ]
        )
        db.commit()

        with pytest.raises(TenantContextResolutionError) as exc_info:
            TenantContextService(db).resolve_for_user(user_id=user.id)

        assert exc_info.value.code == "explicit_tenant_required"
    finally:
        db.close()
        engine.dispose()


def test_resolve_for_user_accepts_explicit_tenant_when_membership_exists() -> None:
    engine, db = _build_session()
    try:
        user = User(username="explicit-membership-user", password="secret")
        tenant_one = Tenant(id=21, slug="tenant-21", name="Tenant 21")
        tenant_two = Tenant(id=22, slug="tenant-22", name="Tenant 22")
        db.add_all([user, tenant_one, tenant_two])
        db.flush()
        membership = TenantMembership(tenant_id=22, user_id=user.id, role="admin")
        db.add_all([TenantMembership(tenant_id=21, user_id=user.id, role="viewer"), membership])
        db.commit()

        resolved = TenantContextService(db).resolve_for_user(
            user_id=user.id,
            requested_tenant_id=22,
            requested_source="header",
        )

        assert resolved is not None
        assert resolved.tenant_id == 22
        assert resolved.membership_id == membership.id
        assert resolved.role == "admin"
        assert resolved.source == "header"
    finally:
        db.close()
        engine.dispose()


def test_resolve_for_user_rejects_requested_tenant_without_membership() -> None:
    engine, db = _build_session()
    try:
        user = User(username="wrong-tenant-user", password="secret")
        tenant_one = Tenant(id=31, slug="tenant-31", name="Tenant 31")
        tenant_two = Tenant(id=32, slug="tenant-32", name="Tenant 32")
        db.add_all([user, tenant_one, tenant_two])
        db.flush()
        db.add(TenantMembership(tenant_id=31, user_id=user.id, role="owner"))
        db.commit()

        with pytest.raises(TenantContextResolutionError) as exc_info:
            TenantContextService(db).resolve_for_user(user_id=user.id, requested_tenant_id=32)

        assert exc_info.value.code == "tenant_membership_required"
    finally:
        db.close()
        engine.dispose()


def test_resolve_for_user_rejects_inactive_membership() -> None:
    engine, db = _build_session()
    try:
        user = User(username="inactive-membership-user", password="secret")
        tenant = Tenant(id=41, slug="tenant-41", name="Tenant 41")
        membership = TenantMembership(tenant_id=41, user_id=1, role="owner")
        db.add_all([user, tenant])
        db.flush()
        membership.user_id = user.id
        db.add(membership)
        db.flush()
        membership.status = "inactive"

        with pytest.raises(TenantContextResolutionError) as exc_info:
            TenantContextService(db).resolve_for_user(user_id=user.id, requested_tenant_id=41)

        assert exc_info.value.code == "inactive_tenant_membership"
    finally:
        db.close()
        engine.dispose()


def test_resolve_for_user_rejects_inactive_tenant_for_explicit_selection() -> None:
    engine, db = _build_session()
    try:
        user = User(username="inactive-tenant-explicit-user", password="secret")
        tenant = Tenant(id=51, slug="tenant-51", name="Tenant 51", status="inactive")
        db.add_all([user, tenant])
        db.flush()
        db.add(TenantMembership(tenant_id=51, user_id=user.id, role="owner"))
        db.commit()

        with pytest.raises(TenantContextResolutionError) as exc_info:
            TenantContextService(db).resolve_for_user(user_id=user.id, requested_tenant_id=51)

        assert exc_info.value.code == "inactive_tenant_membership"
    finally:
        db.close()
        engine.dispose()


def test_resolve_for_user_rejects_inactive_tenant_for_single_membership_selection() -> None:
    engine, db = _build_session()
    try:
        user = User(username="inactive-tenant-single-user", password="secret")
        tenant = Tenant(
            id=52,
            slug="tenant-52",
            name="Tenant 52",
            deactivated_at=datetime(2026, 5, 25, tzinfo=UTC),
        )
        db.add_all([user, tenant])
        db.flush()
        db.add(TenantMembership(tenant_id=52, user_id=user.id, role="owner"))
        db.commit()

        with pytest.raises(TenantContextResolutionError) as exc_info:
            TenantContextService(db).resolve_for_user(user_id=user.id)

        assert exc_info.value.code == "inactive_tenant_membership"
    finally:
        db.close()
        engine.dispose()


def test_bootstrap_creates_default_tenant_without_auto_enrolling_users(
    monkeypatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)
    db = session_factory()
    try:
        db.add_all(
            [
                User(username="bootstrap-user-a", password="secret"),
                User(username="bootstrap-user-b", password="secret"),
            ]
        )
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr("backend.services.tenant.bootstrap.SessionLocal", session_factory)
    bootstrap_default_tenant_state()

    verify = session_factory()
    try:
        tenant = verify.execute(
            select(Tenant).where(Tenant.id == DEFAULT_TENANT_ID)
        ).scalar_one_or_none()
        assert tenant is not None
        memberships = verify.execute(
            select(TenantMembership).where(TenantMembership.tenant_id == DEFAULT_TENANT_ID)
        ).scalars().all()
        assert memberships == []
    finally:
        verify.close()
        engine.dispose()


def test_bootstrap_uses_repair_privileged_rls_scope(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)

    seed = session_factory()
    try:
        seed.add(User(username="bootstrap-privileged-user", password="secret"))
        seed.commit()
    finally:
        seed.close()

    calls: list[tuple[str, str]] = []

    @contextmanager
    def _fake_privileged_bypass(db, *, scope: str, actor_type: str):
        calls.append((scope, actor_type))
        yield

    monkeypatch.setattr("backend.services.tenant.bootstrap.SessionLocal", session_factory)
    monkeypatch.setattr(
        "backend.services.tenant.bootstrap.privileged_rls_bypass",
        _fake_privileged_bypass,
    )

    try:
        bootstrap_default_tenant_state()
        assert calls == [("repair", "system")]
    finally:
        engine.dispose()
