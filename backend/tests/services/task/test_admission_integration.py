"""Integration tests for real admission wiring in create/start task flows.

Responsibilities:
- Exercise real `AdmissionControlService` and `TaskStateService` behavior through
  `TaskLifecycleService.create_task` and `TaskRuntimeService.start_task`.
- Prove reason-code outcomes, local-vs-runner admission enforcement,
  transaction-bound over-admission protection, and runner dispatch boundary
  consistency.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import threading
import uuid as uuid_lib

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.config.generated_config import (
    CLOUD_RUNNER_CONTROL_ENABLED_ENV,
    CONFIG_DIR_ENV,
    DATA_PLANE_OBJECT_STORE_BACKEND_ENV,
    DEPLOYMENT_PROFILE_ENV,
    RUNNER_TOOL_COMMAND_ENABLED_ENV,
    SECRETS_DIR_ENV,
    TASK_RUNTIME_PLACEMENT_MODE_DEFAULT_ENV,
    GeneratedConfigPaths,
    bootstrap_generated_config,
)
from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Engagement, Task, TaskHistory, User
from backend.models.runner_control import ExecutionSite, Runner, RunnerConnection, RunnerCredential
from backend.models.tenant import Tenant
from backend.schemas.vpn import TaskCreateVPN
from backend.services.runtime_provider.contracts import RuntimeCallScope, RuntimeOperationResult, RuntimeOperationStatus
from backend.services.task.admission_service import AdmissionControlService
from backend.services.task.lifecycle_service import TaskLifecycleService
from backend.services.task.runtime_service import TaskRuntimeService
from backend.services.task.state_service import TaskStateService


@dataclass
class _FakeRuntimeResult:
    ok: bool
    error_code: str | None = None


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Engagement.__table__,
            Task.__table__,
            TaskHistory.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerCredential.__table__,
            RunnerConnection.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return factory()


def _build_file_session_factory(db_path: Path) -> sessionmaker:
    engine = create_engine(
        f"sqlite+pysqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Engagement.__table__,
            Task.__table__,
            TaskHistory.__table__,
            ExecutionSite.__table__,
            Runner.__table__,
            RunnerCredential.__table__,
            RunnerConnection.__table__,
        ],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _generated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GeneratedConfigPaths:
    paths = GeneratedConfigPaths(
        config_dir=tmp_path / "config",
        secrets_dir=tmp_path / "secrets",
    )
    monkeypatch.setenv(CONFIG_DIR_ENV, str(paths.config_dir))
    monkeypatch.setenv(SECRETS_DIR_ENV, str(paths.secrets_dir))
    return paths


def _clear_product_policy_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        DEPLOYMENT_PROFILE_ENV,
        TASK_RUNTIME_PLACEMENT_MODE_DEFAULT_ENV,
        CLOUD_RUNNER_CONTROL_ENABLED_ENV,
        RUNNER_TOOL_COMMAND_ENABLED_ENV,
        DATA_PLANE_OBJECT_STORE_BACKEND_ENV,
    ):
        monkeypatch.delenv(key, raising=False)


def _seed_tenant(
    db: Session,
    *,
    slug: str,
    name: str,
    max_concurrent_tasks: int | None,
    max_concurrent_tasks_per_user: int | None,
) -> Tenant:
    tenant = Tenant(
        slug=slug,
        name=name,
        max_concurrent_tasks=max_concurrent_tasks,
        max_concurrent_tasks_per_user=max_concurrent_tasks_per_user,
    )
    db.add(tenant)
    db.flush()
    return tenant


def _seed_user(db: Session, *, username: str, max_concurrent_tasks: int | None) -> User:
    user = User(username=username, password="hashed", max_concurrent_tasks=max_concurrent_tasks)
    db.add(user)
    db.flush()
    return user


def _seed_task(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    name: str,
    status: str,
    runtime_placement_mode: str = "local",
    runner_id: str | None = None,
    execution_site_id: str | None = None,
) -> Task:
    task = Task(
        graph_thread_id=uuid_lib.uuid4().hex,
        tenant_id=tenant_id,
        user_id=user_id,
        name=name,
        status=status,
        runtime_placement_mode=runtime_placement_mode,
        runner_id=runner_id,
        execution_site_id=execution_site_id,
    )
    db.add(task)
    db.flush()
    return task


def _seed_runner_stack(
    db: Session,
    *,
    tenant_id: int,
    max_active_tasks: int | None,
    now: datetime,
) -> Runner:
    site = ExecutionSite(
        tenant_id=tenant_id,
        name=f"site-{uuid_lib.uuid4().hex[:8]}",
        slug=f"site-{uuid_lib.uuid4().hex[:8]}",
        status="active",
    )
    db.add(site)
    db.flush()

    runner = Runner(
        tenant_id=tenant_id,
        execution_site_id=site.id,
        name=f"runner-{uuid_lib.uuid4().hex[:8]}",
        status="active",
        max_active_tasks=max_active_tasks,
        version="1.2.0",
        capabilities_json=["docker"],
        labels_json={},
        capacity_json={"available_tasks": 0, "active_tasks": 999, "max_active_tasks": 999},
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


def _seed_runner_assignment_gap_case(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    reason_code: str,
    now: datetime,
) -> None:
    if reason_code == "NO_RUNNERS_REGISTERED":
        return

    runner = _seed_runner_stack(db, tenant_id=tenant_id, max_active_tasks=2, now=now)
    if reason_code == "RUNNER_NOT_ONLINE":
        runner.status = "offline"
    elif reason_code == "RUNNER_STALE_OR_OFFLINE":
        connection = db.execute(
            select(RunnerConnection).where(RunnerConnection.runner_id == runner.id)
        ).scalar_one()
        connection.lease_expires_at = now - timedelta(seconds=1)
        connection.last_seen_at = now
    elif reason_code == "RUNNER_HEARTBEAT_STALE":
        runner.last_seen_at = now - timedelta(seconds=121)
    elif reason_code == "RUNNER_REVOKED":
        runner.status = "revoked"
    elif reason_code == "RUNNER_MAINTENANCE_MODE":
        runner.status = "maintenance"
    elif reason_code == "RUNNER_CAPACITY_EXHAUSTED":
        runner.max_active_tasks = 1
        _seed_task(
            db,
            tenant_id=tenant_id,
            user_id=user_id,
            name="runner-assignment-gap-active-task",
            status=TaskStatus.RUNNING.value,
            runtime_placement_mode="runner",
            runner_id=str(runner.id),
        )
    else:  # pragma: no cover - guards parametrized test cases.
        raise AssertionError(f"Unhandled runner assignment reason case: {reason_code}")
    db.flush()


def _configure_create_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    service: TaskLifecycleService,
    *,
    placement: str,
) -> None:
    monkeypatch.setattr(
        service,
        "_resolve_task_create_runtime_placement_mode",
        lambda **_kwargs: placement,
    )
    monkeypatch.setattr(service, "materialize_runtime_workspace_for_task", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_queue_and_start_background_init", lambda *_args, **_kwargs: True)


def _configure_start_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    service: TaskRuntimeService,
) -> None:
    async def _noop_materialize(self, **_kwargs):  # noqa: ANN001
        return None

    class _RuntimeOperationStub:
        async def run_authorized_task_operation(self, **_kwargs):
            return _FakeRuntimeResult(ok=True)

    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.TaskLifecycleService.materialize_runtime_workspace_for_task_async",
        _noop_materialize,
    )
    monkeypatch.setattr(service, "_runtime_operations", _RuntimeOperationStub())
    monkeypatch.setattr(service, "_is_runner_assignment_probe_result", lambda **_kwargs: False)
    monkeypatch.setattr(service, "_is_runner_pending_result", lambda **_kwargs: False)


def _assert_http_conflict(exc: HTTPException, *, reason_code: str) -> None:
    assert exc.status_code == 409
    assert isinstance(exc.detail, dict)
    assert exc.detail.get("reason_code") == reason_code
    assert exc.detail.get("reason_codes") == [reason_code]


@pytest.mark.parametrize(
    ("reason_code", "placement"),
    [
        ("USER_QUOTA_EXCEEDED", "local"),
        ("TENANT_QUOTA_EXCEEDED", "local"),
        ("RUNNER_CAPACITY_EXHAUSTED", "runner"),
    ],
)
def test_create_task_returns_expected_admission_reason_codes_real_flow(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
    placement: str,
) -> None:
    db = _build_session()
    now = datetime.now(tz=UTC)
    tenant = _seed_tenant(
        db,
        slug="tenant-create-reject",
        name="Tenant Create Reject",
        max_concurrent_tasks=5,
        max_concurrent_tasks_per_user=5,
    )
    user = _seed_user(db, username="owner-create-reject", max_concurrent_tasks=5)

    if reason_code == "USER_QUOTA_EXCEEDED":
        user.max_concurrent_tasks = 1
        _seed_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            name="active-user-task",
            status=TaskStatus.RUNNING.value,
            runtime_placement_mode="local",
        )
    elif reason_code == "TENANT_QUOTA_EXCEEDED":
        tenant.max_concurrent_tasks = 1
        other_user = _seed_user(db, username="owner-create-tenant-other", max_concurrent_tasks=5)
        _seed_task(
            db,
            tenant_id=tenant.id,
            user_id=other_user.id,
            name="active-tenant-task",
            status=TaskStatus.PAUSED.value,
            runtime_placement_mode="local",
        )
    else:
        runner = _seed_runner_stack(db, tenant_id=tenant.id, max_active_tasks=1, now=now)
        _seed_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            name="runner-active-task",
            status=TaskStatus.RUNNING.value,
            runtime_placement_mode="runner",
            runner_id=str(runner.id),
        )

    db.commit()
    service = TaskLifecycleService(db)
    _configure_create_side_effects(monkeypatch, service, placement=placement)

    with pytest.raises(HTTPException) as exc:
        service.create_task(
            TaskCreateVPN(name=f"create-reject-{reason_code.lower()}"),
            user_id=user.id,
            tenant_context=SimpleNamespace(tenant_id=tenant.id, user_id=user.id, role="owner"),
        )

    _assert_http_conflict(exc.value, reason_code=reason_code)


@pytest.mark.parametrize(
    "reason_code",
    [
        "NO_RUNNERS_REGISTERED",
        "RUNNER_NOT_ONLINE",
        "RUNNER_STALE_OR_OFFLINE",
        "RUNNER_HEARTBEAT_STALE",
        "RUNNER_REVOKED",
        "RUNNER_MAINTENANCE_MODE",
        "RUNNER_CAPACITY_EXHAUSTED",
    ],
)
def test_create_task_preserves_runner_assignment_rejection_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    db = _build_session()
    now = datetime.now(tz=UTC)
    tenant = _seed_tenant(
        db,
        slug=f"tenant-create-assignment-{reason_code.lower().replace('_', '-')}",
        name=f"Tenant Create Assignment {reason_code}",
        max_concurrent_tasks=5,
        max_concurrent_tasks_per_user=5,
    )
    user = _seed_user(
        db,
        username=f"owner-create-assignment-{reason_code.lower().replace('_', '-')}",
        max_concurrent_tasks=5,
    )
    _seed_runner_assignment_gap_case(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        reason_code=reason_code,
        now=now,
    )
    db.commit()

    service = TaskLifecycleService(db)
    _configure_create_side_effects(monkeypatch, service, placement="runner")

    with pytest.raises(HTTPException) as exc:
        service.create_task(
            TaskCreateVPN(name=f"create-assignment-{reason_code.lower()}"),
            user_id=user.id,
            tenant_context=SimpleNamespace(tenant_id=tenant.id, user_id=user.id, role="owner"),
        )

    _assert_http_conflict(exc.value, reason_code=reason_code)


def test_create_task_runner_placement_persists_real_admitted_runner_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _build_session()
    now = datetime.now(tz=UTC)
    tenant = _seed_tenant(
        db,
        slug="tenant-create-runner",
        name="Tenant Create Runner",
        max_concurrent_tasks=5,
        max_concurrent_tasks_per_user=5,
    )
    user = _seed_user(db, username="owner-create-runner", max_concurrent_tasks=5)
    runner = _seed_runner_stack(db, tenant_id=tenant.id, max_active_tasks=2, now=now)
    db.commit()

    service = TaskLifecycleService(db)
    _configure_create_side_effects(monkeypatch, service, placement="runner")

    created = service.create_task(
        TaskCreateVPN(name="create-runner-admitted"),
        user_id=user.id,
        tenant_context=SimpleNamespace(tenant_id=tenant.id, user_id=user.id, role="owner"),
    )

    persisted = db.execute(select(Task).where(Task.id == created.id)).scalar_one()
    assert persisted.runner_id == str(runner.id)
    assert persisted.execution_site_id == str(runner.execution_site_id)


@pytest.mark.parametrize("deployment_profile", ("single_host", "distributed"))
def test_create_task_uses_generated_product_policy_without_process_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deployment_profile: str,
) -> None:
    paths = _generated_paths(tmp_path, monkeypatch)
    _clear_product_policy_process_env(monkeypatch)
    bootstrap_generated_config(
        profile=deployment_profile,
        docker=False,
        paths=paths,
        postgres_host="postgres",
    )
    _clear_product_policy_process_env(monkeypatch)

    db = _build_session()
    now = datetime.now(tz=UTC)
    tenant = _seed_tenant(
        db,
        slug=f"tenant-create-generated-{deployment_profile}",
        name=f"Tenant Create Generated {deployment_profile}",
        max_concurrent_tasks=5,
        max_concurrent_tasks_per_user=5,
    )
    user = _seed_user(
        db,
        username=f"owner-create-generated-{deployment_profile}",
        max_concurrent_tasks=5,
    )
    runner = _seed_runner_stack(db, tenant_id=tenant.id, max_active_tasks=2, now=now)
    db.commit()

    service = TaskLifecycleService(db)
    monkeypatch.setattr(service, "materialize_runtime_workspace_for_task", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_queue_and_start_background_init", lambda *_args, **_kwargs: True)

    created = service.create_task(
        TaskCreateVPN(name=f"create-generated-{deployment_profile}"),
        user_id=user.id,
        tenant_context=SimpleNamespace(tenant_id=tenant.id, user_id=user.id, role="owner"),
    )

    persisted = db.execute(select(Task).where(Task.id == created.id)).scalar_one()
    assert persisted.runtime_placement_mode == "runner"
    assert persisted.runner_id == str(runner.id)
    assert persisted.execution_site_id == str(runner.execution_site_id)


@pytest.mark.parametrize(
    ("reason_code", "placement"),
    [
        ("USER_QUOTA_EXCEEDED", "local"),
        ("TENANT_QUOTA_EXCEEDED", "local"),
        ("RUNNER_CAPACITY_EXHAUSTED", "runner"),
    ],
)
@pytest.mark.asyncio
async def test_start_task_returns_expected_admission_reason_codes_real_flow(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
    placement: str,
) -> None:
    db = _build_session()
    now = datetime.now(tz=UTC)
    tenant = _seed_tenant(
        db,
        slug="tenant-start-reject",
        name="Tenant Start Reject",
        max_concurrent_tasks=5,
        max_concurrent_tasks_per_user=5,
    )
    user = _seed_user(db, username=f"owner-start-reject-{reason_code.lower()}", max_concurrent_tasks=5)

    start_target = _seed_task(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        name=f"start-reject-target-{reason_code.lower()}",
        status=TaskStatus.CREATED.value,
        runtime_placement_mode=placement,
    )

    if reason_code == "USER_QUOTA_EXCEEDED":
        user.max_concurrent_tasks = 1
        _seed_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            name="active-user-start-task",
            status=TaskStatus.RUNNING.value,
            runtime_placement_mode="local",
        )
    elif reason_code == "TENANT_QUOTA_EXCEEDED":
        tenant.max_concurrent_tasks = 1
        other_user = _seed_user(db, username="owner-start-tenant-other", max_concurrent_tasks=5)
        _seed_task(
            db,
            tenant_id=tenant.id,
            user_id=other_user.id,
            name="active-tenant-start-task",
            status=TaskStatus.PAUSED.value,
            runtime_placement_mode="local",
        )
    else:
        runner = _seed_runner_stack(db, tenant_id=tenant.id, max_active_tasks=1, now=now)
        _seed_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            name="active-runner-capacity-task",
            status=TaskStatus.RUNNING.value,
            runtime_placement_mode="runner",
            runner_id=str(runner.id),
        )

    db.commit()

    service = TaskRuntimeService(db)
    _configure_start_side_effects(monkeypatch, service)

    with pytest.raises(HTTPException) as exc:
        await service.start_task(
            task_id=start_target.id,
            user_id=user.id,
            tenant_id=tenant.id,
            runtime_call_scope=RuntimeCallScope.DIAGNOSTIC if placement == "local" else RuntimeCallScope.PRODUCT_TASK,
        )

    _assert_http_conflict(exc.value, reason_code=reason_code)


@pytest.mark.parametrize(
    "reason_code",
    [
        "NO_RUNNERS_REGISTERED",
        "RUNNER_NOT_ONLINE",
        "RUNNER_STALE_OR_OFFLINE",
        "RUNNER_HEARTBEAT_STALE",
        "RUNNER_REVOKED",
        "RUNNER_MAINTENANCE_MODE",
        "RUNNER_CAPACITY_EXHAUSTED",
    ],
)
@pytest.mark.asyncio
async def test_start_task_preserves_runner_assignment_rejection_reason_code(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
) -> None:
    db = _build_session()
    now = datetime.now(tz=UTC)
    tenant = _seed_tenant(
        db,
        slug=f"tenant-start-assignment-{reason_code.lower().replace('_', '-')}",
        name=f"Tenant Start Assignment {reason_code}",
        max_concurrent_tasks=5,
        max_concurrent_tasks_per_user=5,
    )
    user = _seed_user(
        db,
        username=f"owner-start-assignment-{reason_code.lower().replace('_', '-')}",
        max_concurrent_tasks=5,
    )
    start_target = _seed_task(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        name=f"start-assignment-target-{reason_code.lower()}",
        status=TaskStatus.CREATED.value,
        runtime_placement_mode="runner",
    )
    _seed_runner_assignment_gap_case(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        reason_code=reason_code,
        now=now,
    )
    db.commit()

    service = TaskRuntimeService(db)
    _configure_start_side_effects(monkeypatch, service)

    with pytest.raises(HTTPException) as exc:
        await service.start_task(task_id=start_target.id, user_id=user.id, tenant_id=tenant.id)

    _assert_http_conflict(exc.value, reason_code=reason_code)


@pytest.mark.asyncio
async def test_start_task_runner_dispatch_uses_persisted_admitted_runner_at_request_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _build_session()
    now = datetime.now(tz=UTC)
    tenant = _seed_tenant(
        db,
        slug="tenant-start-dispatch",
        name="Tenant Start Dispatch",
        max_concurrent_tasks=5,
        max_concurrent_tasks_per_user=5,
    )
    user = _seed_user(db, username="owner-start-dispatch", max_concurrent_tasks=5)
    runner = _seed_runner_stack(db, tenant_id=tenant.id, max_active_tasks=3, now=now)
    task = _seed_task(
        db,
        tenant_id=tenant.id,
        user_id=user.id,
        name="start-dispatch-target",
        status=TaskStatus.CREATED.value,
        runtime_placement_mode="runner",
        runner_id="stale-runner-id",
        execution_site_id="stale-execution-site-id",
    )
    db.commit()

    dispatched: dict[str, str] = {}

    async def _noop_materialize(self, **_kwargs):  # noqa: ANN001
        return None

    class _ProviderDispatchProbe:
        provider_name = "provider-dispatch-probe"

        async def provision_task_runtime(self, request):
            dispatched["request_runner_id"] = str(request.runner_id)
            dispatched["request_execution_site_id"] = str(request.execution_site_id)
            persisted = db.execute(select(Task).where(Task.id == task.id)).scalar_one()
            dispatched["persisted_runner_id"] = str(persisted.runner_id)
            dispatched["persisted_execution_site_id"] = str(persisted.execution_site_id)
            return RuntimeOperationResult(
                tenant_id=request.tenant_id,
                task_id=request.task_id,
                actor_type=request.actor_type,
                actor_id=request.actor_id,
                runtime_placement_mode=request.runtime_placement_mode,
                workspace_id=request.workspace_id,
                accepted=True,
                provider=self.provider_name,
                operation=request.operation,
                status=RuntimeOperationStatus.SUCCEEDED,
                user_id=request.user_id,
                runner_id=request.runner_id,
                execution_site_id=request.execution_site_id,
            )

    class _RegistryProbe:
        def __init__(self) -> None:
            self._provider = _ProviderDispatchProbe()

        def get_provider(self, runtime_placement_mode):  # noqa: ANN001
            return self._provider

    service = TaskRuntimeService(db)
    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.TaskLifecycleService.materialize_runtime_workspace_for_task_async",
        _noop_materialize,
    )
    monkeypatch.setattr(service._runtime_operations, "_registry", _RegistryProbe())
    monkeypatch.setattr(service, "_is_runner_assignment_probe_result", lambda **_kwargs: False)
    monkeypatch.setattr(service, "_is_runner_pending_result", lambda **_kwargs: False)

    returned = await service.start_task(task_id=task.id, user_id=user.id, tenant_id=tenant.id)

    assert returned.id == task.id
    assert dispatched == {
        "request_runner_id": str(runner.id),
        "request_execution_site_id": str(runner.execution_site_id),
        "persisted_runner_id": str(runner.id),
        "persisted_execution_site_id": str(runner.execution_site_id),
    }


def test_parallel_create_calls_cannot_overadmit_with_real_admission_write_transaction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    factory = _build_file_session_factory(tmp_path / "parallel_create_admission.sqlite3")
    setup_db = factory()
    tenant = _seed_tenant(
        setup_db,
        slug="tenant-parallel-create",
        name="Tenant Parallel Create",
        max_concurrent_tasks=1,
        max_concurrent_tasks_per_user=5,
    )
    user_one = _seed_user(setup_db, username="owner-parallel-create-1", max_concurrent_tasks=5)
    user_two = _seed_user(setup_db, username="owner-parallel-create-2", max_concurrent_tasks=5)
    setup_db.commit()
    tenant_id = tenant.id
    user_ids = [int(user_one.id), int(user_two.id)]
    setup_db.close()

    tenant_locks: dict[int, threading.Lock] = {}
    tenant_locks_guard = threading.Lock()

    def _tenant_lock(self: AdmissionControlService, *, tenant_id: int) -> None:
        with tenant_locks_guard:
            tenant_lock = tenant_locks.setdefault(tenant_id, threading.Lock())
        tenant_lock.acquire()
        self._db.info["_tenant_admission_lock"] = tenant_lock

    monkeypatch.setattr(AdmissionControlService, "_acquire_advisory_xact_lock", _tenant_lock)
    monkeypatch.setattr(
        TaskLifecycleService,
        "_resolve_task_create_runtime_placement_mode",
        lambda self, **_kwargs: "local",
    )

    results: dict[int, tuple[str, str | None]] = {}
    errors: list[BaseException] = []
    start_event = threading.Event()

    def _worker(user_id: int) -> None:
        db = factory()
        service = TaskLifecycleService(db)
        setattr(service, "materialize_runtime_workspace_for_task", lambda **_kwargs: None)
        setattr(service, "_queue_and_start_background_init", lambda *_args, **_kwargs: True)
        start_event.wait(timeout=2)
        try:
            service.create_task(
                TaskCreateVPN(name=f"parallel-create-{user_id}"),
                user_id=user_id,
                tenant_context=SimpleNamespace(tenant_id=tenant_id, user_id=user_id, role="owner"),
            )
            results[user_id] = ("allowed", None)
        except HTTPException as exc:
            reason_code = None
            if isinstance(exc.detail, dict):
                reason_code = str(exc.detail.get("reason_code"))
            results[user_id] = ("denied", reason_code)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            tenant_lock = db.info.pop("_tenant_admission_lock", None)
            if tenant_lock is not None and tenant_lock.locked():
                tenant_lock.release()
            db.close()

    threads = [threading.Thread(target=_worker, args=(user_id,)) for user_id in user_ids]
    for thread in threads:
        thread.start()
    start_event.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert errors == []
    assert len(results) == 2
    assert sum(1 for outcome, _ in results.values() if outcome == "allowed") == 1
    denied_reasons = [reason for outcome, reason in results.values() if outcome == "denied"]
    assert denied_reasons == ["TENANT_QUOTA_EXCEEDED"]

    verify_db = factory()
    active_count = (
        verify_db.query(Task)
        .filter(
            Task.tenant_id == tenant_id,
            Task.status.in_(tuple(TaskStatus.active_task_statuses())),
        )
        .count()
    )
    assert active_count == 1
    verify_db.close()


def test_parallel_start_calls_cannot_overadmit_queued_transition_through_staged_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    factory = _build_file_session_factory(tmp_path / "parallel_start_admission.sqlite3")
    setup_db = factory()
    tenant = _seed_tenant(
        setup_db,
        slug="tenant-parallel-start",
        name="Tenant Parallel Start",
        max_concurrent_tasks=1,
        max_concurrent_tasks_per_user=5,
    )
    user_one = _seed_user(setup_db, username="owner-parallel-start-1", max_concurrent_tasks=5)
    user_two = _seed_user(setup_db, username="owner-parallel-start-2", max_concurrent_tasks=5)
    task_one = _seed_task(
        setup_db,
        tenant_id=tenant.id,
        user_id=user_one.id,
        name="parallel-start-task-1",
        status=TaskStatus.STOPPED.value,
        runtime_placement_mode="local",
    )
    task_two = _seed_task(
        setup_db,
        tenant_id=tenant.id,
        user_id=user_two.id,
        name="parallel-start-task-2",
        status=TaskStatus.STOPPED.value,
        runtime_placement_mode="local",
    )
    setup_db.commit()
    task_ids = [task_one.id, task_two.id]
    tenant_id = tenant.id
    user_ids = [int(user_one.id), int(user_two.id)]
    setup_db.close()

    tenant_locks: dict[int, threading.Lock] = {}
    tenant_locks_guard = threading.Lock()

    def _tenant_lock(self: AdmissionControlService, *, tenant_id: int) -> None:
        with tenant_locks_guard:
            tenant_lock = tenant_locks.setdefault(tenant_id, threading.Lock())
        tenant_lock.acquire()
        self._db.info["_tenant_admission_lock"] = tenant_lock

    original_change_task_status = TaskStateService.change_task_status

    def _guard_change_task_status(
        self,
        task_id: int,
        new_status: str,
        user_id: int | None = None,
        reason: str | None = None,
        change_source: str = "manual",
        metadata: dict | None = None,
    ):
        if new_status == TaskStatus.QUEUED.value:
            raise AssertionError("QUEUED must be staged via stage_task_status_change inside admission callback")
        return original_change_task_status(
            self,
            task_id=task_id,
            new_status=new_status,
            user_id=user_id,
            reason=reason,
            change_source=change_source,
            metadata=metadata,
        )

    async def _noop_materialize(self, **_kwargs):  # noqa: ANN001
        return None

    class _RuntimeOperationStub:
        async def run_authorized_task_operation(self, **_kwargs):
            return _FakeRuntimeResult(ok=True)

    monkeypatch.setattr(AdmissionControlService, "_acquire_advisory_xact_lock", _tenant_lock)
    monkeypatch.setattr(TaskStateService, "change_task_status", _guard_change_task_status)
    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.TaskLifecycleService.materialize_runtime_workspace_for_task_async",
        _noop_materialize,
    )

    results: dict[int, tuple[str, str | None]] = {}
    errors: list[BaseException] = []
    start_event = threading.Event()

    def _worker(task_id: int, user_id: int) -> None:
        db = factory()
        service = TaskRuntimeService(db)
        setattr(service, "_runtime_operations", _RuntimeOperationStub())
        setattr(service, "_is_runner_assignment_probe_result", lambda **_kwargs: False)
        setattr(service, "_is_runner_pending_result", lambda **_kwargs: False)
        start_event.wait(timeout=2)
        try:
            asyncio.run(
                service.start_task(
                    task_id=task_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    runtime_call_scope=RuntimeCallScope.DIAGNOSTIC,
                )
            )
            results[task_id] = ("allowed", None)
        except HTTPException as exc:
            reason_code = None
            if isinstance(exc.detail, dict):
                reason_code = str(exc.detail.get("reason_code"))
            results[task_id] = ("denied", reason_code)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            tenant_lock = db.info.pop("_tenant_admission_lock", None)
            if tenant_lock is not None and tenant_lock.locked():
                tenant_lock.release()
            db.close()

    threads = [
        threading.Thread(target=_worker, args=(task_ids[0], user_ids[0])),
        threading.Thread(target=_worker, args=(task_ids[1], user_ids[1])),
    ]
    for thread in threads:
        thread.start()
    start_event.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert errors == []
    assert len(results) == 2
    assert sum(1 for outcome, _ in results.values() if outcome == "allowed") == 1
    denied_reasons = [reason for outcome, reason in results.values() if outcome == "denied"]
    assert denied_reasons == ["TENANT_QUOTA_EXCEEDED"]

    verify_db = factory()
    tasks = verify_db.query(Task).order_by(Task.id.asc()).all()
    queued_history_count = verify_db.query(TaskHistory).filter(TaskHistory.new_status == TaskStatus.QUEUED.value).count()
    running_count = sum(1 for task in tasks if task.status == TaskStatus.RUNNING.value)
    stopped_count = sum(1 for task in tasks if task.status == TaskStatus.STOPPED.value)
    assert running_count == 1
    assert stopped_count == 1
    assert queued_history_count == 1
    verify_db.close()
