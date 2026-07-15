"""Router tests for `/api/auth/me` tenant context and membership metadata."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models import Tenant, TenantMembership, User
from backend.routers import auth as auth_router


def _build_db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return session_factory()


def _build_client(*, db: Session, current_user: User) -> TestClient:
    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/auth")

    def _db_dep():
        yield db

    app.dependency_overrides[auth_router.get_db] = _db_dep
    app.dependency_overrides[auth_router.get_current_user] = lambda: current_user
    return TestClient(app)


def _build_public_client(*, db: Session) -> TestClient:
    app = FastAPI()
    app.include_router(auth_router.router, prefix="/api/auth")

    def _db_dep():
        yield db

    app.dependency_overrides[auth_router.get_db] = _db_dep
    return TestClient(app)


def test_register_creates_default_tenant_membership_for_token_user() -> None:
    db = _build_db()
    try:
        client = _build_public_client(db=db)
        response = client.post(
            "/api/auth/register",
            json={
                "username": "registered-token-user",
                "password": "secret-password",
                "email": "registered-token-user@example.test",
            },
        )
        assert response.status_code == 201, response.text
        payload = response.json()

        user_id = int(payload["user"]["id"])
        membership = db.execute(
            select(TenantMembership).where(TenantMembership.user_id == user_id)
        ).scalar_one()
        tenant = db.get(Tenant, membership.tenant_id)

        assert payload["access_token"]
        assert tenant is not None
        assert tenant.id == 1
        assert membership.role == "owner"
        assert membership.status == "active"
    finally:
        db.close()


def test_me_returns_active_tenant_and_membership_summary_for_single_membership() -> None:
    db = _build_db()
    try:
        user = User(username="single-membership-auth-user", password="secret")
        tenant = Tenant(id=1, slug="default", name="Default Tenant")
        db.add_all([user, tenant])
        db.flush()
        db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner"))
        db.commit()
        db.refresh(user)

        client = _build_client(db=db, current_user=user)
        response = client.get("/api/auth/me")
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["id"] == user.id
        assert payload["active_tenant"]["tenant_id"] == 1
        assert payload["active_tenant"]["role"] == "owner"
        assert payload["effective_permissions"]["role"] == "owner"
        assert payload["effective_permissions"]["tenant_id"] == 1
        assert payload["membership_summaries"][0]["tenant_id"] == 1
        assert payload["membership_summaries"][0]["tenant_slug"] == "default"
    finally:
        db.close()


def test_me_repairs_default_tenant_membership_for_standalone_user() -> None:
    db = _build_db()
    try:
        user = User(username="standalone-auth-user", password="secret")
        db.add(user)
        db.commit()
        db.refresh(user)

        client = _build_client(db=db, current_user=user)
        response = client.get("/api/auth/me")
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["active_tenant"]["tenant_id"] == 1
        assert payload["active_tenant"]["is_default_tenant"] is True
        assert payload["membership_summaries"][0]["tenant_id"] == 1
        assert payload["membership_summaries"][0]["is_default_tenant"] is True
    finally:
        db.close()


def test_me_does_not_silently_pick_first_tenant_for_multi_membership_user() -> None:
    db = _build_db()
    try:
        user = User(username="multi-membership-auth-user", password="secret")
        tenant_a = Tenant(id=7, slug="tenant-seven", name="Tenant Seven")
        tenant_b = Tenant(id=8, slug="tenant-eight", name="Tenant Eight")
        db.add_all([user, tenant_a, tenant_b])
        db.flush()
        db.add_all(
            [
                TenantMembership(tenant_id=7, user_id=user.id, role="viewer"),
                TenantMembership(tenant_id=8, user_id=user.id, role="admin"),
            ]
        )
        db.commit()
        db.refresh(user)

        client = _build_client(db=db, current_user=user)
        response = client.get("/api/auth/me")
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["active_tenant"] is None
        assert payload["effective_permissions"] is None
        assert [item["tenant_id"] for item in payload["membership_summaries"]] == [7, 8]
    finally:
        db.close()


def test_me_honors_membership_validated_tenant_header_hint() -> None:
    db = _build_db()
    try:
        user = User(username="header-membership-auth-user", password="secret")
        tenant_a = Tenant(id=17, slug="tenant-17", name="Tenant Seventeen")
        tenant_b = Tenant(id=18, slug="tenant-18", name="Tenant Eighteen")
        db.add_all([user, tenant_a, tenant_b])
        db.flush()
        db.add_all(
            [
                TenantMembership(tenant_id=17, user_id=user.id, role="viewer"),
                TenantMembership(tenant_id=18, user_id=user.id, role="admin"),
            ]
        )
        db.commit()
        db.refresh(user)

        client = _build_client(db=db, current_user=user)
        response = client.get("/api/auth/me", headers={"X-Active-Tenant-Id": "18"})
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["active_tenant"]["tenant_id"] == 18
        assert payload["active_tenant"]["source"] == "header"
        assert payload["effective_permissions"]["role"] == "admin"
    finally:
        db.close()


def test_me_rejects_non_member_tenant_header_hint() -> None:
    db = _build_db()
    try:
        user = User(username="wrong-header-membership-user", password="secret")
        tenant_a = Tenant(id=27, slug="tenant-27", name="Tenant Twenty Seven")
        tenant_b = Tenant(id=28, slug="tenant-28", name="Tenant Twenty Eight")
        db.add_all([user, tenant_a, tenant_b])
        db.flush()
        db.add(TenantMembership(tenant_id=27, user_id=user.id, role="owner"))
        db.commit()
        db.refresh(user)

        client = _build_client(db=db, current_user=user)
        response = client.get("/api/auth/me", headers={"X-Active-Tenant-Id": "28"})
        assert response.status_code == 403, response.text
        assert "not associated" in response.json()["detail"]
    finally:
        db.close()
