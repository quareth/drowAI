"""Tests for Runner Control runner assignment eligibility and deterministic selection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Engagement, Task, User
from backend.models.runner_control import ExecutionSite, Runner, RunnerConnection, RunnerCredential
from backend.models.tenant import Tenant
from backend.services.runner_control.assignment_service import RunnerAssignmentRequest, RunnerAssignmentService


class _MetricsStub:
    def __init__(self) -> None:
        self.success_count = 0
        self.failure_reason_sets: list[tuple[str, ...]] = []

    def record_assignment_success(self) -> None:
        self.success_count += 1

    def record_assignment_failure(self, *, reason_codes) -> None:
        self.failure_reason_sets.append(tuple(reason_codes))


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Engagement.__table__,
            Task.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerCredential.__table__,
            RunnerConnection.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def _seed_tenants(db: Session) -> tuple[Tenant, Tenant]:
    tenant_one = Tenant(slug="tenant-one", name="Tenant One")
    tenant_two = Tenant(slug="tenant-two", name="Tenant Two")
    user = User(username="owner", password="hashed")
    db.add_all([tenant_one, tenant_two, user])
    db.flush()
    return tenant_one, tenant_two


def _seed_site(db: Session, *, tenant_id: int, name: str, slug: str) -> ExecutionSite:
    site = ExecutionSite(
        tenant_id=tenant_id,
        name=name,
        slug=slug,
        status="active",
    )
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
    lease_seconds: int = 90,
    include_credential: bool = True,
    credential_status: str = "active",
    credential_revoked: bool = False,
    available_tasks: int = 1,
    active_tasks: int = 0,
    max_active_tasks: int = 4,
    labels: dict[str, str] | None = None,
    capabilities: list[str] | None = None,
    runner_version: str = "1.2.0",
    protocol_version: str = "runner_control.v1",
    last_seen_at: datetime | None = None,
) -> Runner:
    runner = Runner(
        tenant_id=tenant_id,
        execution_site_id=site_id,
        name=name,
        status=status,
        max_active_tasks=max_active_tasks,
        version=runner_version,
        labels_json=labels or {},
        capabilities_json=capabilities or [],
        capacity_json={
            "available_tasks": available_tasks,
            "active_tasks": active_tasks,
            "max_active_tasks": max_active_tasks,
            "version": runner_version,
            "protocol_version": protocol_version,
        },
        last_seen_at=last_seen_at or now,
    )
    db.add(runner)
    db.flush()

    db.add(
        RunnerConnection(
            tenant_id=tenant_id,
            runner_id=runner.id,
            pod_id="pod-a",
            connection_id=f"conn-{name}",
            status="active",
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            last_seen_at=last_seen_at or now,
        )
    )

    if include_credential:
        db.add(
            RunnerCredential(
                tenant_id=tenant_id,
                runner_id=runner.id,
                credential_fingerprint=f"fp-{name}",
                secret_hash="sha256$deadbeef",
                status=credential_status,
                revoked_at=(now if credential_revoked else None),
                expires_at=now + timedelta(days=30),
            )
        )

    db.flush()

    if active_tasks > 0:
        owner_user_id = db.execute(select(User.id).order_by(User.id.asc()).limit(1)).scalar_one()
        for index in range(active_tasks):
            db.add(
                Task(
                    user_id=owner_user_id,
                    tenant_id=tenant_id,
                    name=f"{name}-active-{index}",
                    status=TaskStatus.RUNNING.value,
                    runner_id=str(runner.id),
                    execution_site_id=str(site_id),
                    runtime_placement_mode="runner",
                )
            )
        db.flush()

    return runner


def test_select_runner_never_selects_another_tenant() -> None:
    db = _build_session()
    now = datetime(2026, 5, 23, 9, 0, tzinfo=UTC)
    tenant_one, tenant_two = _seed_tenants(db)
    site_one = _seed_site(db, tenant_id=tenant_one.id, name="Site One", slug="site-one")
    site_two = _seed_site(db, tenant_id=tenant_two.id, name="Site Two", slug="site-two")

    _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_one.id,
        name="tenant-one-stale",
        now=now,
        lease_seconds=-60,
    )
    tenant_two_runner = _seed_runner(
        db,
        tenant_id=tenant_two.id,
        site_id=site_two.id,
        name="tenant-two-healthy",
        now=now,
        lease_seconds=180,
        available_tasks=3,
    )
    db.commit()

    service = RunnerAssignmentService(db)

    tenant_one_result = service.select_runner(RunnerAssignmentRequest(tenant_id=tenant_one.id), now=now)
    assert tenant_one_result.selection is None
    assert "RUNNER_STALE_OR_OFFLINE" in tenant_one_result.reason_codes

    tenant_two_result = service.select_runner(RunnerAssignmentRequest(tenant_id=tenant_two.id), now=now)
    assert tenant_two_result.selection is not None
    assert tenant_two_result.selection.runner_id == tenant_two_runner.id


def test_select_runner_excludes_offline_or_stale_lease_and_stale_heartbeat() -> None:
    db = _build_session()
    now = datetime(2026, 5, 23, 10, 0, tzinfo=UTC)
    tenant_one, _tenant_two = _seed_tenants(db)
    site_one = _seed_site(db, tenant_id=tenant_one.id, name="Site One", slug="site-one")

    healthy = _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_one.id,
        name="healthy",
        now=now,
        lease_seconds=120,
        available_tasks=1,
        last_seen_at=now,
    )
    _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_one.id,
        name="stale-lease",
        now=now,
        lease_seconds=-1,
        available_tasks=10,
        last_seen_at=now,
    )
    _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_one.id,
        name="stale-heartbeat",
        now=now,
        lease_seconds=120,
        available_tasks=10,
        last_seen_at=now - timedelta(minutes=10),
    )
    db.commit()

    service = RunnerAssignmentService(db, heartbeat_stale_after=timedelta(seconds=90))
    result = service.select_runner(RunnerAssignmentRequest(tenant_id=tenant_one.id), now=now)

    assert result.selection is not None
    assert result.selection.runner_id == healthy.id


def test_select_runner_uses_execution_site_capabilities_labels_and_versions() -> None:
    db = _build_session()
    now = datetime(2026, 5, 23, 11, 0, tzinfo=UTC)
    tenant_one, _tenant_two = _seed_tenants(db)
    site_one = _seed_site(db, tenant_id=tenant_one.id, name="Primary", slug="primary")
    site_two = _seed_site(db, tenant_id=tenant_one.id, name="Secondary", slug="secondary")

    _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_two.id,
        name="wrong-site",
        now=now,
        lease_seconds=120,
        available_tasks=10,
        labels={"region": "eu"},
        capabilities=["docker", "pty"],
        runner_version="1.0.0",
        protocol_version="runner_control.v0",
    )

    selected = _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_one.id,
        name="selected",
        now=now,
        lease_seconds=120,
        available_tasks=2,
        labels={"region": "eu", "tier": "prod"},
        capabilities=["docker", "pty", "artifact_upload"],
        runner_version="1.3.0",
        protocol_version="runner_control.v1",
    )
    db.commit()

    service = RunnerAssignmentService(db)
    result = service.select_runner(
        RunnerAssignmentRequest(
            tenant_id=tenant_one.id,
            execution_site_id=site_one.id,
            required_protocol_version="runner_control.v1",
            required_runtime_version=">=1.2.0",
            required_capabilities=("docker", "artifact_upload"),
            required_labels={"tier": "prod"},
        ),
        now=now,
    )

    assert result.selection is not None
    assert result.selection.runner_id == selected.id


def test_select_runner_returns_stable_reason_codes_when_no_runner_is_eligible() -> None:
    db = _build_session()
    now = datetime(2026, 5, 23, 12, 0, tzinfo=UTC)
    tenant_one, _tenant_two = _seed_tenants(db)
    site_one = _seed_site(db, tenant_id=tenant_one.id, name="Primary", slug="primary")

    _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_one.id,
        name="revoked-credential",
        now=now,
        include_credential=False,
    )
    _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_one.id,
        name="capability-mismatch",
        now=now,
        capabilities=["pty"],
    )
    _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_one.id,
        name="capacity-exhausted",
        now=now,
        capabilities=["docker"],
        available_tasks=0,
        active_tasks=4,
        max_active_tasks=4,
    )
    db.commit()

    service = RunnerAssignmentService(db)
    result = service.select_runner(
        RunnerAssignmentRequest(
            tenant_id=tenant_one.id,
            required_capabilities=("docker",),
            minimum_available_tasks=1,
        ),
        now=now,
    )

    assert result.selection is None
    assert result.reason_codes == (
        "RUNNER_CAPABILITY_MISMATCH",
        "RUNNER_CAPACITY_EXHAUSTED",
        "RUNNER_CREDENTIAL_NOT_ACTIVE",
    )


def test_select_runner_preserves_no_runner_registered_reason_code() -> None:
    db = _build_session()
    now = datetime(2026, 5, 23, 12, 30, tzinfo=UTC)
    tenant_one, _tenant_two = _seed_tenants(db)
    db.commit()

    service = RunnerAssignmentService(db)
    result = service.select_runner(RunnerAssignmentRequest(tenant_id=tenant_one.id), now=now)

    assert result.selection is None
    assert result.reason_codes == ("NO_RUNNERS_REGISTERED",)
    assert result.evaluated_runner_count == 0


def test_select_runner_preserves_expired_active_lease_reason_code() -> None:
    db = _build_session()
    now = datetime(2026, 5, 23, 12, 35, tzinfo=UTC)
    tenant_one, _tenant_two = _seed_tenants(db)
    site_one = _seed_site(db, tenant_id=tenant_one.id, name="Primary", slug="primary")
    _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_one.id,
        name="expired-lease",
        now=now,
        lease_seconds=-1,
        available_tasks=1,
    )
    db.commit()

    service = RunnerAssignmentService(db)
    result = service.select_runner(RunnerAssignmentRequest(tenant_id=tenant_one.id), now=now)

    assert result.selection is None
    assert result.reason_codes == ("RUNNER_STALE_OR_OFFLINE",)
    assert result.evaluated_runner_count == 1


@pytest.mark.parametrize(
    ("runner_status", "reason_code"),
    [
        ("revoked", "RUNNER_REVOKED"),
        ("maintenance", "RUNNER_MAINTENANCE_MODE"),
    ],
)
def test_select_runner_preserves_unavailable_runner_status_reason_codes(
    runner_status: str,
    reason_code: str,
) -> None:
    db = _build_session()
    now = datetime(2026, 5, 23, 12, 40, tzinfo=UTC)
    tenant_one, _tenant_two = _seed_tenants(db)
    site_one = _seed_site(db, tenant_id=tenant_one.id, name="Primary", slug="primary")
    _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_one.id,
        name=f"{runner_status}-runner",
        now=now,
        status=runner_status,
        available_tasks=1,
    )
    db.commit()

    service = RunnerAssignmentService(db)
    result = service.select_runner(RunnerAssignmentRequest(tenant_id=tenant_one.id), now=now)

    assert result.selection is None
    assert result.reason_codes == (reason_code,)
    assert result.evaluated_runner_count == 1


def test_select_runner_preserves_capacity_exhausted_reason_code() -> None:
    db = _build_session()
    now = datetime(2026, 5, 23, 12, 45, tzinfo=UTC)
    tenant_one, _tenant_two = _seed_tenants(db)
    site_one = _seed_site(db, tenant_id=tenant_one.id, name="Primary", slug="primary")
    _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_one.id,
        name="capacity-exhausted",
        now=now,
        active_tasks=1,
        available_tasks=0,
        max_active_tasks=1,
    )
    db.commit()

    service = RunnerAssignmentService(db)
    result = service.select_runner(RunnerAssignmentRequest(tenant_id=tenant_one.id), now=now)

    assert result.selection is None
    assert result.reason_codes == ("RUNNER_CAPACITY_EXHAUSTED",)
    assert result.evaluated_runner_count == 1


def test_select_runner_prefers_highest_available_capacity_deterministically() -> None:
    db = _build_session()
    now = datetime(2026, 5, 23, 13, 0, tzinfo=UTC)
    tenant_one, _tenant_two = _seed_tenants(db)
    site_one = _seed_site(db, tenant_id=tenant_one.id, name="Primary", slug="primary")

    low = _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_one.id,
        name="low-cap",
        now=now,
        active_tasks=3,
        available_tasks=1,
        lease_seconds=300,
    )
    high = _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_one.id,
        name="high-cap",
        now=now,
        available_tasks=4,
        lease_seconds=60,
    )
    db.commit()

    service = RunnerAssignmentService(db)
    result = service.select_runner(RunnerAssignmentRequest(tenant_id=tenant_one.id), now=now)

    assert result.selection is not None
    assert result.selection.runner_id == high.id
    assert result.selection.runner_id != low.id


def test_select_runner_reports_assignment_failure_metrics() -> None:
    db = _build_session()
    now = datetime(2026, 5, 23, 14, 0, tzinfo=UTC)
    tenant_one, _tenant_two = _seed_tenants(db)
    metrics = _MetricsStub()

    service = RunnerAssignmentService(db, metrics=metrics)
    result = service.select_runner(RunnerAssignmentRequest(tenant_id=tenant_one.id), now=now)

    assert result.selection is None
    assert metrics.failure_reason_sets == [("NO_RUNNERS_REGISTERED",)]
    assert metrics.success_count == 0


def test_select_runner_reports_assignment_success_metrics() -> None:
    db = _build_session()
    now = datetime(2026, 5, 23, 15, 0, tzinfo=UTC)
    tenant_one, _tenant_two = _seed_tenants(db)
    site_one = _seed_site(db, tenant_id=tenant_one.id, name="Primary", slug="primary")
    _seed_runner(
        db,
        tenant_id=tenant_one.id,
        site_id=site_one.id,
        name="healthy",
        now=now,
        available_tasks=2,
    )
    db.commit()

    metrics = _MetricsStub()
    service = RunnerAssignmentService(db, metrics=metrics)
    result = service.select_runner(RunnerAssignmentRequest(tenant_id=tenant_one.id), now=now)

    assert result.selection is not None
    assert metrics.success_count == 1
    assert metrics.failure_reason_sets == []
