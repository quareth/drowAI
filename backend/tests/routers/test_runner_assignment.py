"""Router tests for Runner Control task-to-runner assignment management endpoints.

Scope:
- Verifies authenticated tenant-scoped assignment API behavior and runtime-job
  read/list routes introduced for Task 5.4.

Boundaries:
- Focuses on router/service integration with in-memory SQLite state only.
- Does not exercise remote runtime execution, dispatch, or runner side effects.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Iterator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database import Base
from backend.models.core import Engagement, Task, User
from backend.models.runner_control import (
    ExecutionSite,
    Runner,
    RunnerConnection,
    RunnerControlMessage,
    RunnerCredential,
    RuntimeJob,
)
from backend.models.tenant import Tenant, TenantMembership
from backend.routers import runner_control as runner_routes


@contextmanager
def _make_db() -> Iterator[Session]:
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
            RuntimeJob.__table__,
            RunnerControlMessage.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()


def _seed_user(db: Session, *, username: str) -> User:
    user = User(username=username, password="hashed")
    db.add(user)
    db.flush()
    return user


def _seed_tenant_with_membership(db: Session, *, user: User, slug: str, name: str) -> Tenant:
    tenant = Tenant(slug=slug, name=name)
    db.add(tenant)
    db.flush()
    db.add(TenantMembership(tenant_id=tenant.id, user_id=user.id, role="owner"))
    db.flush()
    return tenant


def _seed_task(db: Session, *, tenant: Tenant, user: User, suffix: str) -> Task:
    engagement = Engagement(
        user_id=user.id,
        tenant_id=tenant.id,
        name=f"Engagement {suffix}",
        status="active",
    )
    db.add(engagement)
    db.flush()

    task = Task(
        user_id=user.id,
        tenant_id=tenant.id,
        engagement_id=engagement.id,
        name=f"Task {suffix}",
        status="created",
    )
    db.add(task)
    db.flush()
    return task


def _seed_eligible_runner(db: Session, *, tenant: Tenant, suffix: str, now: datetime) -> Runner:
    site = ExecutionSite(
        tenant_id=tenant.id,
        name=f"Primary Site {suffix}",
        slug=f"primary-site-{suffix}",
        status="active",
    )
    db.add(site)
    db.flush()

    runner = Runner(
        tenant_id=tenant.id,
        execution_site_id=site.id,
        name=f"runner-{suffix}",
        status="active",
        version="1.3.0",
        capabilities_json=["docker"],
        labels_json={"region": "us-east"},
        capacity_json={
            "active_tasks": 0,
            "max_active_tasks": 4,
            "available_tasks": 4,
            "protocol_version": "runner_control.v1",
            "version": "1.3.0",
        },
        last_seen_at=now,
    )
    db.add(runner)
    db.flush()

    db.add(
        RunnerCredential(
            tenant_id=tenant.id,
            runner_id=runner.id,
            credential_fingerprint=f"fp-{suffix}",
            secret_hash="sha256$deadbeef",
            status="active",
            expires_at=now + timedelta(days=7),
        )
    )
    db.add(
        RunnerConnection(
            tenant_id=tenant.id,
            runner_id=runner.id,
            pod_id="pod-a",
            connection_id=f"conn-{suffix}",
            status="active",
            lease_expires_at=now + timedelta(minutes=5),
            last_seen_at=now,
        )
    )
    db.flush()
    return runner


@contextmanager
def _make_client(db: Session, *, current_user: object | None = None) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(runner_routes.router)

    def _fake_get_db():
        yield db

    app.dependency_overrides[runner_routes.get_db] = _fake_get_db

    if current_user is not None:

        def _fake_get_current_user():
            return current_user

        app.dependency_overrides[runner_routes.get_current_user] = _fake_get_current_user

    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_assign_runner_requires_authenticated_user() -> None:
    with _make_db() as db:
        with _make_client(db, current_user=None) as client:
            response = client.post("/api/runner-control/tasks/1/assign-runner", json={})

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials"


def test_assign_runner_enforces_task_access_boundary() -> None:
    now = datetime.now(tz=UTC)
    with _make_db() as db:
        owner = _seed_user(db, username="owner")
        intruder = _seed_user(db, username="intruder")
        tenant = _seed_tenant_with_membership(db, user=owner, slug="tenant-one", name="Tenant One")
        _seed_tenant_with_membership(db, user=intruder, slug="tenant-two", name="Tenant Two")
        task = _seed_task(db, tenant=tenant, user=owner, suffix="owner")
        _seed_eligible_runner(db, tenant=tenant, suffix="one", now=now)
        db.commit()

        with _make_client(db, current_user=SimpleNamespace(id=intruder.id, username=intruder.username)) as client:
            response = client.post(f"/api/runner-control/tasks/{task.id}/assign-runner", json={})

    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found"}


def test_assign_runner_creates_runtime_job_updates_task_and_enqueues_probe_message() -> None:
    now = datetime.now(tz=UTC)
    with _make_db() as db:
        owner = _seed_user(db, username="owner")
        tenant = _seed_tenant_with_membership(db, user=owner, slug="tenant-one", name="Tenant One")
        task = _seed_task(db, tenant=tenant, user=owner, suffix="assignment")
        runner = _seed_eligible_runner(db, tenant=tenant, suffix="one", now=now)
        db.commit()

        with _make_client(db, current_user=SimpleNamespace(id=owner.id, username=owner.username)) as client:
            response = client.post(
                f"/api/runner-control/tasks/{task.id}/assign-runner",
                json={
                    "idempotency_key": "assign-task-1",
                    "required_capabilities": ["docker"],
                    "payload_json": {"probe": "runner_control"},
                },
            )

        assert response.status_code == 201
        payload = response.json()
        assert payload["task_id"] == task.id
        assert payload["runner_id"] == str(runner.id)
        assert payload["execution_site_id"] == str(runner.execution_site_id)
        assert payload["runtime_job_status"] == "assigned"
        assert payload["idempotency_key"] == "assign-task-1"

        refreshed_task = db.execute(select(Task).where(Task.id == task.id)).scalar_one()
        assert refreshed_task.runner_id == str(runner.id)
        assert refreshed_task.execution_site_id == str(runner.execution_site_id)

        runtime_job = db.execute(select(RuntimeJob).where(RuntimeJob.idempotency_key == "assign-task-1")).scalar_one()
        assert runtime_job.tenant_id == tenant.id
        assert runtime_job.task_id == task.id
        assert runtime_job.runner_id == runner.id
        assert runtime_job.status == "assigned"

        outbound_count = db.execute(select(func.count()).select_from(RunnerControlMessage)).scalar_one()
        assert outbound_count == 1
        outbound_message = db.execute(
            select(RunnerControlMessage).where(
                RunnerControlMessage.tenant_id == tenant.id,
                RunnerControlMessage.runner_id == runner.id,
                RunnerControlMessage.runtime_job_id == runtime_job.id,
                RunnerControlMessage.direction == "outbound",
            )
        ).scalar_one()
        assert outbound_message.type == "runner.assignment.probe"
        assert outbound_message.status == "queued"
        assert outbound_message.idempotency_key == f"probe:{runtime_job.id}"


def test_assign_runner_returns_409_when_no_eligible_runner_and_preserves_task_metadata() -> None:
    with _make_db() as db:
        owner = _seed_user(db, username="owner")
        tenant = _seed_tenant_with_membership(db, user=owner, slug="tenant-one", name="Tenant One")
        task = _seed_task(db, tenant=tenant, user=owner, suffix="no-runner")
        db.commit()

        with _make_client(db, current_user=SimpleNamespace(id=owner.id, username=owner.username)) as client:
            response = client.post(f"/api/runner-control/tasks/{task.id}/assign-runner", json={})

        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["error_code"] == "NO_ELIGIBLE_RUNNER"
        assert detail["reason_codes"] == ["NO_RUNNERS_REGISTERED"]

        refreshed_task = db.execute(select(Task).where(Task.id == task.id)).scalar_one()
        assert refreshed_task.runner_id is None
        assert refreshed_task.execution_site_id is None

        runtime_jobs = db.execute(select(RuntimeJob).where(RuntimeJob.task_id == task.id)).scalars().all()
        assert runtime_jobs == []


def test_runtime_job_read_and_list_endpoints_are_task_scoped() -> None:
    now = datetime.now(tz=UTC)
    with _make_db() as db:
        owner = _seed_user(db, username="owner")
        intruder = _seed_user(db, username="intruder")
        tenant = _seed_tenant_with_membership(db, user=owner, slug="tenant-one", name="Tenant One")
        _seed_tenant_with_membership(db, user=intruder, slug="tenant-two", name="Tenant Two")
        task = _seed_task(db, tenant=tenant, user=owner, suffix="runtime-job")
        _seed_eligible_runner(db, tenant=tenant, suffix="one", now=now)
        db.commit()

        with _make_client(db, current_user=SimpleNamespace(id=owner.id, username=owner.username)) as client:
            assign_response = client.post(
                f"/api/runner-control/tasks/{task.id}/assign-runner",
                json={"idempotency_key": "runtime-job-lookup"},
            )

            assert assign_response.status_code == 201
            runtime_job_id = assign_response.json()["runtime_job_id"]

            get_response = client.get(f"/api/runner-control/runtime-jobs/{runtime_job_id}")
            assert get_response.status_code == 200
            assert get_response.json()["id"] == runtime_job_id

            list_response = client.get(f"/api/runner-control/runtime-jobs?task_id={task.id}")
            assert list_response.status_code == 200
            assert [row["id"] for row in list_response.json()] == [runtime_job_id]

        with _make_client(db, current_user=SimpleNamespace(id=intruder.id, username=intruder.username)) as client:
            blocked_get = client.get(f"/api/runner-control/runtime-jobs/{runtime_job_id}")
            blocked_list = client.get(f"/api/runner-control/runtime-jobs?task_id={task.id}")

    assert blocked_get.status_code == 404
    assert blocked_get.json() == {"detail": "Runtime job not found."}
    assert blocked_list.status_code == 404
    assert blocked_list.json() == {"detail": "Task not found"}


def test_runtime_job_list_requires_task_id_and_blocks_same_tenant_non_owner_access() -> None:
    now = datetime.now(tz=UTC)
    with _make_db() as db:
        owner = _seed_user(db, username="owner")
        teammate = _seed_user(db, username="teammate")
        tenant = _seed_tenant_with_membership(db, user=owner, slug="tenant-one", name="Tenant One")
        db.add(TenantMembership(tenant_id=tenant.id, user_id=teammate.id, role="member"))
        task = _seed_task(db, tenant=tenant, user=owner, suffix="same-tenant-owner")
        _seed_eligible_runner(db, tenant=tenant, suffix="one", now=now)
        db.commit()

        with _make_client(db, current_user=SimpleNamespace(id=owner.id, username=owner.username)) as client:
            assign_response = client.post(
                f"/api/runner-control/tasks/{task.id}/assign-runner",
                json={"idempotency_key": "same-tenant-runtime-job"},
            )
            assert assign_response.status_code == 201

        with _make_client(db, current_user=SimpleNamespace(id=teammate.id, username=teammate.username)) as client:
            unfiltered_list = client.get("/api/runner-control/runtime-jobs")
            filtered_list = client.get(f"/api/runner-control/runtime-jobs?task_id={task.id}")

    assert unfiltered_list.status_code == 422
    assert unfiltered_list.json()["detail"][0]["loc"] == ["query", "task_id"]
    assert filtered_list.status_code == 404
    assert filtered_list.json() == {"detail": "Task not found"}
