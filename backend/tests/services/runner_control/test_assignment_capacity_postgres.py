"""Postgres-sourced runner capacity tests for runner assignment.

This module verifies that runner capacity gating uses task counts from the
control-plane database (`tasks`) together with `runner.max_active_tasks` and
ignores stale heartbeat `capacity_json` values for admission decisions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid as uuid_lib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Task, User
from backend.models.runner_control import ExecutionSite, Runner, RunnerConnection, RunnerCredential
from backend.models.tenant import Tenant
from backend.services.runner_control.assignment_service import RunnerAssignmentRequest, RunnerAssignmentService


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Task.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerCredential.__table__,
            RunnerConnection.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def _seed_tenant_and_user(db: Session) -> tuple[Tenant, User]:
    tenant = Tenant(slug="tenant-capacity", name="Tenant Capacity")
    user = User(username="owner-capacity", password="hashed")
    db.add_all([tenant, user])
    db.flush()
    return tenant, user


def _seed_site(db: Session, *, tenant_id: int) -> ExecutionSite:
    site = ExecutionSite(tenant_id=tenant_id, name="Site", slug="site", status="active")
    db.add(site)
    db.flush()
    return site


def _seed_runner(
    db: Session,
    *,
    tenant_id: int,
    site_id,
    now: datetime,
    max_active_tasks: int | None,
    capacity_json: dict[str, int] | None = None,
) -> Runner:
    runner = Runner(
        tenant_id=tenant_id,
        execution_site_id=site_id,
        name=f"runner-{uuid_lib.uuid4().hex[:8]}",
        status="active",
        max_active_tasks=max_active_tasks,
        version="1.2.0",
        labels_json={},
        capabilities_json=["docker"],
        capacity_json=capacity_json or {"available_tasks": 0, "active_tasks": 999, "max_active_tasks": 999},
        last_seen_at=now,
    )
    db.add(runner)
    db.flush()

    db.add(
        RunnerCredential(
            tenant_id=tenant_id,
            runner_id=runner.id,
            credential_fingerprint=f"fp-{uuid_lib.uuid4().hex[:8]}",
            secret_hash="sha256$deadbeef",
            status="active",
            revoked_at=None,
            expires_at=now + timedelta(days=30),
        )
    )
    db.add(
        RunnerConnection(
            tenant_id=tenant_id,
            runner_id=runner.id,
            pod_id="pod-a",
            connection_id=f"conn-{uuid_lib.uuid4().hex[:8]}",
            status="active",
            lease_expires_at=now + timedelta(seconds=180),
            last_seen_at=now,
        )
    )
    db.flush()
    return runner


def _seed_active_task(db: Session, *, tenant_id: int, user_id: int, runner_id: str) -> Task:
    task = Task(
        graph_thread_id=uuid_lib.uuid4().hex,
        user_id=user_id,
        tenant_id=tenant_id,
        name=f"task-{uuid_lib.uuid4().hex[:8]}",
        status=TaskStatus.RUNNING.value,
        runtime_placement_mode="runner",
        runner_id=runner_id,
    )
    db.add(task)
    db.flush()
    return task


def test_runner_capacity_exhausted_when_active_count_reaches_ceiling() -> None:
    db = _build_session()
    now = datetime(2026, 5, 29, 12, 0, tzinfo=UTC)
    tenant, user = _seed_tenant_and_user(db)
    site = _seed_site(db, tenant_id=tenant.id)
    runner = _seed_runner(db, tenant_id=tenant.id, site_id=site.id, now=now, max_active_tasks=2)
    _seed_active_task(db, tenant_id=tenant.id, user_id=user.id, runner_id=str(runner.id))
    _seed_active_task(db, tenant_id=tenant.id, user_id=user.id, runner_id=str(runner.id))
    db.commit()

    result = RunnerAssignmentService(db).select_runner(RunnerAssignmentRequest(tenant_id=tenant.id), now=now)

    assert result.selection is None
    assert "RUNNER_CAPACITY_EXHAUSTED" in result.reason_codes


def test_runner_capacity_available_when_active_count_below_ceiling() -> None:
    db = _build_session()
    now = datetime(2026, 5, 29, 12, 30, tzinfo=UTC)
    tenant, user = _seed_tenant_and_user(db)
    site = _seed_site(db, tenant_id=tenant.id)
    runner = _seed_runner(db, tenant_id=tenant.id, site_id=site.id, now=now, max_active_tasks=2)
    _seed_active_task(db, tenant_id=tenant.id, user_id=user.id, runner_id=str(runner.id))
    db.commit()

    result = RunnerAssignmentService(db).select_runner(RunnerAssignmentRequest(tenant_id=tenant.id), now=now)

    assert result.selection is not None
    assert result.selection.runner_id == runner.id


def test_capacity_query_matches_string_runner_id_and_ignores_stale_capacity_json() -> None:
    db = _build_session()
    now = datetime(2026, 5, 29, 13, 0, tzinfo=UTC)
    tenant, _user = _seed_tenant_and_user(db)
    site = _seed_site(db, tenant_id=tenant.id)
    runner = _seed_runner(
        db,
        tenant_id=tenant.id,
        site_id=site.id,
        now=now,
        max_active_tasks=1,
        capacity_json={"available_tasks": 0, "active_tasks": 999, "max_active_tasks": 999},
    )
    db.commit()

    # No active task row exists for this runner ID in `tasks`, so stale heartbeat
    # capacity JSON must not block selection.
    result = RunnerAssignmentService(db).select_runner(RunnerAssignmentRequest(tenant_id=tenant.id), now=now)

    assert result.selection is not None
    assert result.selection.runner_id == runner.id


def test_runner_capacity_falls_back_to_global_default_when_runner_limit_is_null(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_MAX_ACTIVE_TASKS", "1")
    db = _build_session()
    now = datetime(2026, 5, 29, 13, 30, tzinfo=UTC)
    tenant, user = _seed_tenant_and_user(db)
    site = _seed_site(db, tenant_id=tenant.id)
    runner = _seed_runner(db, tenant_id=tenant.id, site_id=site.id, now=now, max_active_tasks=None)
    _seed_active_task(db, tenant_id=tenant.id, user_id=user.id, runner_id=str(runner.id))
    db.commit()

    result = RunnerAssignmentService(db).select_runner(RunnerAssignmentRequest(tenant_id=tenant.id), now=now)

    assert result.selection is None
    assert "RUNNER_CAPACITY_EXHAUSTED" in result.reason_codes


def test_runner_capacity_is_unlimited_when_runner_and_global_limits_are_null(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCAL_MAX_ACTIVE_TASKS", raising=False)
    db = _build_session()
    now = datetime(2026, 5, 29, 14, 0, tzinfo=UTC)
    tenant, user = _seed_tenant_and_user(db)
    site = _seed_site(db, tenant_id=tenant.id)
    runner = _seed_runner(db, tenant_id=tenant.id, site_id=site.id, now=now, max_active_tasks=None)
    _seed_active_task(db, tenant_id=tenant.id, user_id=user.id, runner_id=str(runner.id))
    _seed_active_task(db, tenant_id=tenant.id, user_id=user.id, runner_id=str(runner.id))
    db.commit()

    result = RunnerAssignmentService(db).select_runner(RunnerAssignmentRequest(tenant_id=tenant.id), now=now)

    assert result.selection is not None
    assert result.selection.runner_id == runner.id
    assert result.selection.available_tasks > 0
