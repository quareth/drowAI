"""Tests for product-facing Runner Control readiness service and route behavior."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Iterator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.runner_control import ExecutionSite, Runner, RunnerConnection, RunnerCredential
from backend.models.tenant import Tenant, TenantMembership
from backend.routers import runner_control as runner_routes
from backend.services.runner_control.assignment_service import RunnerAssignmentRequest
from backend.services.runner_control.readiness_service import RunnerReadinessService
from backend.services.tenant.context import TenantRequestContext


@contextmanager
def _build_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            TenantMembership.__table__,
            Engagement.__table__,
            Task.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerCredential.__table__,
            RunnerConnection.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()


def _seed_tenants(db: Session) -> tuple[Tenant, Tenant, User]:
    tenant_one = Tenant(slug="tenant-one", name="Tenant One")
    tenant_two = Tenant(slug="tenant-two", name="Tenant Two")
    user = User(username="owner", password="hashed")
    db.add_all([tenant_one, tenant_two, user])
    db.flush()
    db.add(TenantMembership(tenant_id=tenant_one.id, user_id=user.id, role="owner"))
    db.flush()
    return tenant_one, tenant_two, user


def _seed_site(db: Session, *, tenant_id: int, name: str, slug: str, status: str = "active") -> ExecutionSite:
    site = ExecutionSite(tenant_id=tenant_id, name=name, slug=slug, status=status)
    db.add(site)
    db.flush()
    return site


def _seed_runner(
    db: Session,
    *,
    tenant_id: int,
    site_id,
    name: str,
    now: datetime,
    status: str = "active",
    lease_seconds: int = 300,
    max_active_tasks: int | None = 4,
    capabilities: list[str] | None = None,
    runner_version: str = "1.3.0",
    protocol_version: str = "runner_control.v1",
) -> Runner:
    runner = Runner(
        tenant_id=tenant_id,
        execution_site_id=site_id,
        name=name,
        status=status,
        version=runner_version,
        max_active_tasks=max_active_tasks,
        capabilities_json=capabilities or ["docker"],
        labels_json={},
        capacity_json={
            "protocol_version": protocol_version,
            "version": runner_version,
        },
        last_seen_at=now,
    )
    db.add(runner)
    db.flush()
    db.add(
        RunnerCredential(
            tenant_id=tenant_id,
            runner_id=runner.id,
            credential_fingerprint=f"fp-{name}",
            secret_hash="sha256$deadbeef",
            status="active",
            expires_at=now + timedelta(days=7),
        )
    )
    db.add(
        RunnerConnection(
            tenant_id=tenant_id,
            runner_id=runner.id,
            pod_id="pod-a",
            connection_id=f"conn-{name}",
            status="active",
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            last_seen_at=now,
        )
    )
    db.flush()
    return runner


def test_readiness_ready_uses_assignment_selection_and_connectivity() -> None:
    now = datetime(2026, 5, 24, 10, 0, tzinfo=UTC)
    with _build_session() as db:
        tenant, _other_tenant, _user = _seed_tenants(db)
        site = _seed_site(db, tenant_id=tenant.id, name="Primary", slug="primary")
        runner = _seed_runner(db, tenant_id=tenant.id, site_id=site.id, name="ready", now=now)
        db.commit()

        result = RunnerReadinessService(db).get_readiness(RunnerAssignmentRequest(tenant_id=tenant.id), now=now)

    assert result.status == "ready"
    assert result.ready is True
    assert result.reason_codes == ()
    assert result.selected_runner_id == runner.id
    assert result.execution_site_id == site.id
    assert result.connected_runner_count == 1


def test_readiness_waiting_for_runner_preserves_no_runner_reason_code() -> None:
    now = datetime(2026, 5, 24, 10, 5, tzinfo=UTC)
    with _build_session() as db:
        tenant, _other_tenant, _user = _seed_tenants(db)
        db.commit()

        result = RunnerReadinessService(db).get_readiness(RunnerAssignmentRequest(tenant_id=tenant.id), now=now)

    assert result.status == "waiting_for_runner"
    assert result.ready is False
    assert result.reason_codes == ("NO_RUNNERS_REGISTERED",)


def test_readiness_registered_offline_uses_assignment_reason_codes() -> None:
    now = datetime(2026, 5, 24, 10, 10, tzinfo=UTC)
    with _build_session() as db:
        tenant, _other_tenant, _user = _seed_tenants(db)
        site = _seed_site(db, tenant_id=tenant.id, name="Primary", slug="primary")
        _seed_runner(db, tenant_id=tenant.id, site_id=site.id, name="expired", now=now, lease_seconds=-1)
        db.commit()

        result = RunnerReadinessService(db).get_readiness(RunnerAssignmentRequest(tenant_id=tenant.id), now=now)

    assert result.status == "runner_registered_offline"
    assert result.ready is False
    assert result.reason_codes == ("RUNNER_STALE_OR_OFFLINE",)
    assert result.connected_runner_count == 0


def test_readiness_incompatible_preserves_detailed_reason_codes() -> None:
    now = datetime(2026, 5, 24, 10, 15, tzinfo=UTC)
    with _build_session() as db:
        tenant, _other_tenant, _user = _seed_tenants(db)
        site = _seed_site(db, tenant_id=tenant.id, name="Primary", slug="primary")
        _seed_runner(db, tenant_id=tenant.id, site_id=site.id, name="basic", now=now, capabilities=["pty"])
        db.commit()

        result = RunnerReadinessService(db).get_readiness(
            RunnerAssignmentRequest(tenant_id=tenant.id, required_capabilities=("docker",)),
            now=now,
        )

    assert result.status == "runner_incompatible"
    assert result.ready is False
    assert result.reason_codes == ("RUNNER_CAPABILITY_MISMATCH",)
    assert result.connected_runner_count == 1


def test_readiness_capacity_exhausted_preserves_capacity_reason_code() -> None:
    now = datetime(2026, 5, 24, 10, 20, tzinfo=UTC)
    with _build_session() as db:
        tenant, _other_tenant, _user = _seed_tenants(db)
        site = _seed_site(db, tenant_id=tenant.id, name="Primary", slug="primary")
        _seed_runner(db, tenant_id=tenant.id, site_id=site.id, name="full", now=now, max_active_tasks=0)
        db.commit()

        result = RunnerReadinessService(db).get_readiness(RunnerAssignmentRequest(tenant_id=tenant.id), now=now)

    assert result.status == "runner_capacity_exhausted"
    assert result.ready is False
    assert result.reason_codes == ("RUNNER_CAPACITY_EXHAUSTED",)


def test_readiness_route_returns_current_tenant_scope_only() -> None:
    now = datetime(2026, 5, 24, 10, 30, tzinfo=UTC)
    with _build_session() as db:
        tenant, other_tenant, user = _seed_tenants(db)
        other_site = _seed_site(db, tenant_id=other_tenant.id, name="Other", slug="other")
        _seed_runner(db, tenant_id=other_tenant.id, site_id=other_site.id, name="other-ready", now=now)
        db.commit()

        app = FastAPI()
        app.include_router(runner_routes.router)

        def _fake_get_db():
            yield db

        app.dependency_overrides[runner_routes.get_db] = _fake_get_db
        app.dependency_overrides[runner_routes.get_tenant_request_context] = lambda: TenantRequestContext(
            tenant_id=tenant.id,
            user_id=user.id,
            role="owner",
            membership_id=1,
            is_default_tenant=False,
        )

        with TestClient(app) as client:
            response = client.get("/api/runner-control/readiness")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "waiting_for_runner"
    assert payload["ready"] is False
    assert payload["reason_codes"] == ["NO_RUNNERS_REGISTERED"]
    assert payload["connected_runner_count"] == 0
