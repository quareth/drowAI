"""Router tests for Tenant Isolation tenant membership management and tenant context APIs."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import backend.auth as auth_module
import backend.database as database_module
from backend.database import Base
from backend.models import Tenant, TenantMembership, User
from backend.routers import tenants as tenants_router


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
    app.include_router(tenants_router.router)

    def _db_dep():
        yield db

    app.dependency_overrides[database_module.get_db] = _db_dep
    app.dependency_overrides[tenants_router.get_db] = _db_dep
    app.dependency_overrides[auth_module.get_current_user] = lambda: current_user
    app.dependency_overrides[tenants_router.get_current_user] = lambda: current_user
    return TestClient(app)


def _add_membership(db: Session, *, tenant: Tenant, user: User, role: str) -> TenantMembership:
    membership = TenantMembership(tenant_id=tenant.id, user_id=user.id, role=role)
    db.add(membership)
    db.flush()
    return membership


def test_membership_list_is_scoped_to_authenticated_user() -> None:
    db = _build_db()
    try:
        user_a = User(username="tenant-router-user-a", password="secret")
        user_b = User(username="tenant-router-user-b", password="secret")
        tenant_a = Tenant(id=101, slug="tenant-101", name="Tenant 101")
        tenant_b = Tenant(id=102, slug="tenant-102", name="Tenant 102")
        db.add_all([user_a, user_b, tenant_a, tenant_b])
        db.flush()

        _add_membership(db, tenant=tenant_a, user=user_a, role="owner")
        _add_membership(db, tenant=tenant_b, user=user_b, role="owner")
        db.commit()
        db.refresh(user_a)

        client = _build_client(db=db, current_user=user_a)
        response = client.get("/api/tenants/memberships")
        assert response.status_code == 200, response.text
        payload = response.json()

        assert len(payload) == 1
        assert payload[0]["tenant_id"] == 101
    finally:
        db.close()


def test_default_tenant_is_bootstrapped_for_standalone_user_membership_list() -> None:
    db = _build_db()
    try:
        user = User(username="standalone-tenant-user", password="secret")
        db.add(user)
        db.commit()
        db.refresh(user)

        client = _build_client(db=db, current_user=user)
        response = client.get("/api/tenants/memberships")
        assert response.status_code == 200, response.text
        payload = response.json()

        assert len(payload) == 1
        assert payload[0]["tenant_id"] == 1
        assert payload[0]["is_default_tenant"] is True
    finally:
        db.close()


def test_membership_mutation_requires_owner_or_admin() -> None:
    db = _build_db()
    try:
        viewer = User(username="viewer-membership-manager", password="secret")
        target = User(username="target-membership-manager", password="secret")
        owner = User(username="owner-membership-manager", password="secret")
        tenant = Tenant(id=201, slug="tenant-201", name="Tenant 201")
        db.add_all([viewer, target, owner, tenant])
        db.flush()

        _add_membership(db, tenant=tenant, user=viewer, role="viewer")
        target_membership = _add_membership(db, tenant=tenant, user=target, role="viewer")
        _add_membership(db, tenant=tenant, user=owner, role="owner")
        db.commit()
        db.refresh(viewer)

        client = _build_client(db=db, current_user=viewer)
        response = client.patch(
            f"/api/tenants/{tenant.id}/memberships/{target_membership.id}",
            headers={"X-Active-Tenant-Id": str(tenant.id)},
            json={"role": "admin"},
        )
        assert response.status_code == 403, response.text
    finally:
        db.close()


def test_cannot_remove_last_owner_membership() -> None:
    db = _build_db()
    try:
        owner = User(username="single-owner-user", password="secret")
        tenant = Tenant(id=301, slug="tenant-301", name="Tenant 301")
        db.add_all([owner, tenant])
        db.flush()
        owner_membership = _add_membership(db, tenant=tenant, user=owner, role="owner")
        db.commit()
        db.refresh(owner)

        client = _build_client(db=db, current_user=owner)
        response = client.patch(
            f"/api/tenants/{tenant.id}/memberships/{owner_membership.id}",
            headers={"X-Active-Tenant-Id": str(tenant.id)},
            json={"deactivate": True},
        )
        assert response.status_code == 409, response.text
        assert "at least one owner" in response.json()["detail"]
    finally:
        db.close()


def test_tenant_context_effective_permissions_change_after_switch_role_change_and_deactivation() -> None:
    db = _build_db()
    try:
        owner = User(username="owner-context-user", password="secret")
        subject = User(username="subject-context-user", password="secret")
        tenant_a = Tenant(id=401, slug="tenant-401", name="Tenant 401")
        tenant_b = Tenant(id=402, slug="tenant-402", name="Tenant 402")
        db.add_all([owner, subject, tenant_a, tenant_b])
        db.flush()

        _add_membership(db, tenant=tenant_a, user=owner, role="owner")
        _add_membership(db, tenant=tenant_b, user=owner, role="owner")
        _add_membership(db, tenant=tenant_a, user=subject, role="viewer")
        subject_tenant_b = _add_membership(db, tenant=tenant_b, user=subject, role="viewer")
        db.commit()
        db.refresh(owner)
        db.refresh(subject)

        subject_client = _build_client(db=db, current_user=subject)

        switch_a = subject_client.post("/api/tenants/context/switch", json={"tenant_id": tenant_a.id})
        assert switch_a.status_code == 200, switch_a.text
        payload_a = switch_a.json()
        assert payload_a["effective_permissions"]["role"] == "viewer"
        assert "tenant.membership.manage" not in payload_a["effective_permissions"]["actions"]

        owner_client = _build_client(db=db, current_user=owner)
        role_change = owner_client.patch(
            f"/api/tenants/{tenant_b.id}/memberships/{subject_tenant_b.id}",
            headers={"X-Active-Tenant-Id": str(tenant_b.id)},
            json={"role": "admin"},
        )
        assert role_change.status_code == 200, role_change.text
        assert role_change.json()["role"] == "admin"

        switch_b = subject_client.post("/api/tenants/context/switch", json={"tenant_id": tenant_b.id})
        assert switch_b.status_code == 200, switch_b.text
        payload_b = switch_b.json()
        assert payload_b["effective_permissions"]["role"] == "admin"
        assert "tenant.membership.manage" in payload_b["effective_permissions"]["actions"]

        deactivate = owner_client.patch(
            f"/api/tenants/{tenant_b.id}/memberships/{subject_tenant_b.id}",
            headers={"X-Active-Tenant-Id": str(tenant_b.id)},
            json={"deactivate": True},
        )
        assert deactivate.status_code == 200, deactivate.text
        assert deactivate.json()["status"] == "inactive"
        stored_membership = db.execute(
            select(TenantMembership).where(TenantMembership.id == subject_tenant_b.id)
        ).scalar_one()
        assert stored_membership.status == "inactive"
        assert stored_membership.deactivated_at is not None
        assert stored_membership.deactivated_by_user_id == owner.id

        denied_switch = subject_client.post("/api/tenants/context/switch", json={"tenant_id": tenant_b.id})
        assert denied_switch.status_code == 403, denied_switch.text
    finally:
        db.close()
