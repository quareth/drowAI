"""Unit tests for task admission orchestration with quota and capacity gates.

This module validates AdmissionControlService gate order, reason codes, and
transaction rollback behavior for staged status updates.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import threading
import time
from typing import cast
import uuid as uuid_lib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from backend.database import Base
from backend.domain.task_lifecycle import TaskStatus
from backend.models.core import Task, TaskHistory, User
from backend.models.tenant import Tenant
from backend.services.runner_control.assignment_service import (
    RunnerAssignmentResult,
    RunnerAssignmentService,
    RunnerSelection,
)
from backend.services.task.admission_service import AdmissionControlService
from backend.services.task.state_service import TaskStateService


class _AssignmentServiceStub:
    def __init__(self, result: RunnerAssignmentResult) -> None:
        self._result = result
        self.select_calls = 0

    def select_runner(self, _request) -> RunnerAssignmentResult:
        self.select_calls += 1
        return self._result


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        bind=engine,
        tables=[
            Tenant.__table__,
            User.__table__,
            Task.__table__,
            TaskHistory.__table__,
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
            Task.__table__,
            TaskHistory.__table__,
        ],
    )
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _seed_tenant(
    db: Session,
    *,
    slug: str,
    name: str,
    max_concurrent_tasks: int | None = None,
    max_concurrent_tasks_per_user: int | None = None,
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


def _seed_user(db: Session, *, username: str, max_concurrent_tasks: int | None = None) -> User:
    user = User(username=username, password="hashed", max_concurrent_tasks=max_concurrent_tasks)
    db.add(user)
    db.flush()
    return user


def _seed_task(
    db: Session,
    *,
    tenant_id: int,
    user_id: int,
    status: str,
    runner_id: str | None = None,
    execution_site_id: str | None = None,
) -> Task:
    task = Task(
        graph_thread_id=uuid_lib.uuid4().hex,
        user_id=user_id,
        tenant_id=tenant_id,
        name=f"task-{uuid_lib.uuid4().hex[:8]}",
        status=status,
        runner_id=runner_id,
        execution_site_id=execution_site_id,
    )
    db.add(task)
    db.flush()
    return task


def test_admit_task_allows_local_create_when_under_limits() -> None:
    db = _build_session()
    tenant = _seed_tenant(db, slug="tenant-one", name="Tenant One", max_concurrent_tasks=5, max_concurrent_tasks_per_user=3)
    user = _seed_user(db, username="owner", max_concurrent_tasks=2)
    db.commit()

    service = AdmissionControlService(db)

    def _write_task(_selection: RunnerSelection | None) -> Task:
        task = _seed_task(db, tenant_id=tenant.id, user_id=user.id, status=TaskStatus.CREATED.value)
        return task

    result = service.admit_task(
        tenant_id=tenant.id,
        user_id=user.id,
        placement="local",
        write_task=_write_task,
    )

    assert result.decision.allowed is True
    assert result.task is not None
    assert db.query(Task).filter(Task.tenant_id == tenant.id).count() == 1


def test_admit_task_rejects_user_quota_before_tenant_quota() -> None:
    db = _build_session()
    tenant = _seed_tenant(db, slug="tenant-one", name="Tenant One", max_concurrent_tasks=5, max_concurrent_tasks_per_user=4)
    user = _seed_user(db, username="owner", max_concurrent_tasks=1)
    _seed_task(db, tenant_id=tenant.id, user_id=user.id, status=TaskStatus.RUNNING.value)
    db.commit()

    service = AdmissionControlService(db)
    callback_called = False

    def _write_task(_selection: RunnerSelection | None) -> Task:
        nonlocal callback_called
        callback_called = True
        return _seed_task(db, tenant_id=tenant.id, user_id=user.id, status=TaskStatus.CREATED.value)

    result = service.admit_task(
        tenant_id=tenant.id,
        user_id=user.id,
        placement="local",
        write_task=_write_task,
    )

    assert result.decision.allowed is False
    assert result.decision.reason_code == "USER_QUOTA_EXCEEDED"
    assert callback_called is False


def test_admit_task_rejects_tenant_quota_when_user_has_capacity() -> None:
    db = _build_session()
    tenant = _seed_tenant(db, slug="tenant-one", name="Tenant One", max_concurrent_tasks=1, max_concurrent_tasks_per_user=10)
    user_one = _seed_user(db, username="owner-one", max_concurrent_tasks=5)
    user_two = _seed_user(db, username="owner-two", max_concurrent_tasks=5)
    _seed_task(db, tenant_id=tenant.id, user_id=user_two.id, status=TaskStatus.PAUSED.value)
    db.commit()

    service = AdmissionControlService(db)

    result = service.admit_task(
        tenant_id=tenant.id,
        user_id=user_one.id,
        placement="local",
        write_task=lambda _selection: _seed_task(
            db,
            tenant_id=tenant.id,
            user_id=user_one.id,
            status=TaskStatus.CREATED.value,
        ),
    )

    assert result.decision.allowed is False
    assert result.decision.reason_code == "TENANT_QUOTA_EXCEEDED"


def test_admit_task_runner_mode_persists_admitted_runner_selection() -> None:
    db = _build_session()
    tenant = _seed_tenant(db, slug="tenant-one", name="Tenant One", max_concurrent_tasks=5, max_concurrent_tasks_per_user=5)
    user = _seed_user(db, username="owner", max_concurrent_tasks=5)
    db.commit()

    runner_selection = RunnerSelection(
        runner_id=uuid_lib.uuid4(),
        execution_site_id=uuid_lib.uuid4(),
        available_tasks=2,
        lease_expires_at=datetime(2026, 5, 29, 12, 0, tzinfo=UTC),
    )
    assignment_stub = _AssignmentServiceStub(
        RunnerAssignmentResult(selection=runner_selection, reason_codes=(), evaluated_runner_count=1)
    )
    service = AdmissionControlService(
        db,
        assignment_service_factory=lambda _db: cast(RunnerAssignmentService, assignment_stub),
    )

    def _write_task(selection: RunnerSelection | None) -> Task:
        assert selection is not None
        return _seed_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            status=TaskStatus.CREATED.value,
            runner_id=str(selection.runner_id),
            execution_site_id=str(selection.execution_site_id),
        )

    result = service.admit_task(
        tenant_id=tenant.id,
        user_id=user.id,
        placement="runner",
        write_task=_write_task,
    )

    assert result.decision.allowed is True
    assert result.task is not None
    assert str(result.task.runner_id) == str(runner_selection.runner_id)
    assert str(result.task.execution_site_id) == str(runner_selection.execution_site_id)
    assert assignment_stub.select_calls == 1


def test_admit_task_runner_mode_rejects_when_runner_capacity_exhausted() -> None:
    db = _build_session()
    tenant = _seed_tenant(db, slug="tenant-one", name="Tenant One", max_concurrent_tasks=5, max_concurrent_tasks_per_user=5)
    user = _seed_user(db, username="owner", max_concurrent_tasks=5)
    db.commit()

    assignment_stub = _AssignmentServiceStub(
        RunnerAssignmentResult(
            selection=None,
            reason_codes=("RUNNER_CAPACITY_EXHAUSTED",),
            evaluated_runner_count=1,
        )
    )
    service = AdmissionControlService(
        db,
        assignment_service_factory=lambda _db: cast(RunnerAssignmentService, assignment_stub),
    )
    callback_called = False

    def _write_task(_selection: RunnerSelection | None) -> Task:
        nonlocal callback_called
        callback_called = True
        return _seed_task(db, tenant_id=tenant.id, user_id=user.id, status=TaskStatus.CREATED.value)

    result = service.admit_task(
        tenant_id=tenant.id,
        user_id=user.id,
        placement="runner",
        write_task=_write_task,
    )

    assert result.decision.allowed is False
    assert result.decision.reason_code == "RUNNER_CAPACITY_EXHAUSTED"
    assert result.decision.reason_codes == ("RUNNER_CAPACITY_EXHAUSTED",)
    assert callback_called is False
    assert assignment_stub.select_calls == 1


def test_admit_task_rollback_keeps_staged_start_status_uncommitted_on_error() -> None:
    db = _build_session()
    tenant = _seed_tenant(db, slug="tenant-one", name="Tenant One", max_concurrent_tasks=5, max_concurrent_tasks_per_user=5)
    user = _seed_user(db, username="owner", max_concurrent_tasks=5)
    task = _seed_task(db, tenant_id=tenant.id, user_id=user.id, status=TaskStatus.STOPPED.value)
    db.commit()

    service = AdmissionControlService(db)
    state_service = TaskStateService(db)

    def _write_task(_selection: RunnerSelection | None) -> Task:
        ok, message, _history = state_service.stage_task_status_change(
            task_id=task.id,
            new_status=TaskStatus.QUEUED.value,
            user_id=user.id,
            reason="Queued for startup admission",
            change_source="system",
            metadata={"admission": "start"},
        )
        assert ok is True, message
        raise RuntimeError("simulate post-stage failure")

    with pytest.raises(RuntimeError, match="simulate post-stage failure"):
        service.admit_task(
            tenant_id=tenant.id,
            user_id=user.id,
            placement="local",
            write_task=_write_task,
        )

    db.expire_all()
    persisted = db.query(Task).filter(Task.id == task.id).one()
    history_count = db.query(TaskHistory).filter(TaskHistory.task_id == task.id).count()
    assert persisted.status == TaskStatus.STOPPED.value
    assert history_count == 0


def test_admit_task_local_rejects_when_global_capacity_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_MAX_ACTIVE_TASKS", "1")
    db = _build_session()
    tenant_one = _seed_tenant(db, slug="tenant-one", name="Tenant One", max_concurrent_tasks=10, max_concurrent_tasks_per_user=10)
    tenant_two = _seed_tenant(db, slug="tenant-two", name="Tenant Two", max_concurrent_tasks=10, max_concurrent_tasks_per_user=10)
    user_one = _seed_user(db, username="owner-one", max_concurrent_tasks=10)
    user_two = _seed_user(db, username="owner-two", max_concurrent_tasks=10)
    # An active task in a *different* tenant must still consume the deployment-wide ceiling.
    _seed_task(db, tenant_id=tenant_two.id, user_id=user_two.id, status=TaskStatus.RUNNING.value)
    db.commit()

    service = AdmissionControlService(db)
    callback_called = False

    def _write_task(_selection: RunnerSelection | None) -> Task:
        nonlocal callback_called
        callback_called = True
        return _seed_task(db, tenant_id=tenant_one.id, user_id=user_one.id, status=TaskStatus.CREATED.value)

    result = service.admit_task(
        tenant_id=tenant_one.id,
        user_id=user_one.id,
        placement="local",
        write_task=_write_task,
    )

    assert result.decision.allowed is False
    assert result.decision.reason_code == "GLOBAL_CAPACITY_EXHAUSTED"
    assert callback_called is False


def test_admit_task_local_allows_when_under_global_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_MAX_ACTIVE_TASKS", "5")
    db = _build_session()
    tenant = _seed_tenant(db, slug="tenant-one", name="Tenant One", max_concurrent_tasks=10, max_concurrent_tasks_per_user=10)
    user = _seed_user(db, username="owner", max_concurrent_tasks=10)
    _seed_task(db, tenant_id=tenant.id, user_id=user.id, status=TaskStatus.RUNNING.value)
    db.commit()

    service = AdmissionControlService(db)

    result = service.admit_task(
        tenant_id=tenant.id,
        user_id=user.id,
        placement="local",
        write_task=lambda _selection: _seed_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            status=TaskStatus.CREATED.value,
        ),
    )

    assert result.decision.allowed is True
    assert result.task is not None


def test_admit_task_local_acquires_global_lock_before_tenant_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_MAX_ACTIVE_TASKS", "5")
    db = _build_session()
    tenant = _seed_tenant(db, slug="tenant-one", name="Tenant One", max_concurrent_tasks=10, max_concurrent_tasks_per_user=10)
    user = _seed_user(db, username="owner", max_concurrent_tasks=10)
    db.commit()
    tenant_id = tenant.id

    lock_order: list[str] = []
    service = AdmissionControlService(db)
    monkeypatch.setattr(service, "_acquire_global_capacity_lock", lambda: lock_order.append("global"))
    monkeypatch.setattr(
        service,
        "_acquire_advisory_xact_lock",
        lambda *, tenant_id: lock_order.append(f"tenant:{tenant_id}"),
    )

    result = service.admit_task(
        tenant_id=tenant_id,
        user_id=user.id,
        placement="local",
        write_task=lambda _selection: _seed_task(
            db,
            tenant_id=tenant_id,
            user_id=user.id,
            status=TaskStatus.CREATED.value,
        ),
    )

    assert result.decision.allowed is True
    assert lock_order == ["global", f"tenant:{tenant_id}"]


def test_admit_task_runner_placement_skips_global_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_MAX_ACTIVE_TASKS", "5")
    db = _build_session()
    tenant = _seed_tenant(db, slug="tenant-one", name="Tenant One", max_concurrent_tasks=10, max_concurrent_tasks_per_user=10)
    user = _seed_user(db, username="owner", max_concurrent_tasks=10)
    db.commit()

    runner_selection = RunnerSelection(
        runner_id=uuid_lib.uuid4(),
        execution_site_id=uuid_lib.uuid4(),
        available_tasks=1,
        lease_expires_at=datetime(2026, 5, 29, 12, 0, tzinfo=UTC),
    )
    assignment_stub = _AssignmentServiceStub(
        RunnerAssignmentResult(selection=runner_selection, reason_codes=(), evaluated_runner_count=1)
    )
    service = AdmissionControlService(
        db,
        assignment_service_factory=lambda _db: cast(RunnerAssignmentService, assignment_stub),
    )
    lock_order: list[str] = []
    monkeypatch.setattr(service, "_acquire_global_capacity_lock", lambda: lock_order.append("global"))
    monkeypatch.setattr(
        service,
        "_acquire_advisory_xact_lock",
        lambda *, tenant_id: lock_order.append("tenant"),
    )

    result = service.admit_task(
        tenant_id=tenant.id,
        user_id=user.id,
        placement="runner",
        write_task=lambda selection: _seed_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            status=TaskStatus.CREATED.value,
            runner_id=str(selection.runner_id),
            execution_site_id=str(selection.execution_site_id),
        ),
    )

    assert result.decision.allowed is True
    assert lock_order == ["tenant"]


def test_admit_task_uses_postgres_tenant_scoped_advisory_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _build_session()
    tenant = _seed_tenant(db, slug="tenant-one", name="Tenant One", max_concurrent_tasks=3, max_concurrent_tasks_per_user=3)
    tenant_id = tenant.id
    db.commit()

    class _PostgresBind:
        class dialect:
            name = "postgresql"

    advisory_lock_calls: list[dict[str, int]] = []
    original_execute = db.execute

    def _spy_execute(statement, params=None, *args, **kwargs):  # noqa: ANN001
        if "pg_advisory_xact_lock" in str(statement):
            advisory_lock_calls.append(dict(params or {}))
            return None
        return original_execute(statement, params, *args, **kwargs)

    monkeypatch.setattr(db, "get_bind", lambda *args, **kwargs: _PostgresBind())
    monkeypatch.setattr(db, "execute", _spy_execute)

    service = AdmissionControlService(db)
    service._acquire_advisory_xact_lock(tenant_id=tenant_id)

    assert len(advisory_lock_calls) == 1
    assert advisory_lock_calls[0]["namespace_key"] == 1729
    assert advisory_lock_calls[0]["tenant_key"] == tenant_id


def test_admit_task_sqlite_advisory_lock_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _build_session()
    tenant = _seed_tenant(db, slug="tenant-one", name="Tenant One", max_concurrent_tasks=3, max_concurrent_tasks_per_user=3)
    user = _seed_user(db, username="owner", max_concurrent_tasks=3)
    db.commit()

    advisory_lock_attempted = False
    original_execute = db.execute

    def _spy_execute(statement, params=None, *args, **kwargs):  # noqa: ANN001
        nonlocal advisory_lock_attempted
        if "pg_advisory_xact_lock" in str(statement):
            advisory_lock_attempted = True
        return original_execute(statement, params, *args, **kwargs)

    monkeypatch.setattr(db, "execute", _spy_execute)

    service = AdmissionControlService(db)
    result = service.admit_task(
        tenant_id=tenant.id,
        user_id=user.id,
        placement="local",
        write_task=lambda _selection: _seed_task(
            db,
            tenant_id=tenant.id,
            user_id=user.id,
            status=TaskStatus.CREATED.value,
        ),
    )

    assert result.decision.allowed is True
    assert advisory_lock_attempted is False


def test_parallel_same_tenant_users_do_not_overadmit_under_tenant_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _build_file_session_factory(tmp_path / "admission_parallel.sqlite3")
    setup_db = factory()
    tenant = _seed_tenant(setup_db, slug="tenant-one", name="Tenant One", max_concurrent_tasks=1, max_concurrent_tasks_per_user=5)
    user_one = _seed_user(setup_db, username="owner-one", max_concurrent_tasks=5)
    user_two = _seed_user(setup_db, username="owner-two", max_concurrent_tasks=5)
    setup_db.commit()
    tenant_id = tenant.id
    user_ids = [user_one.id, user_two.id]
    setup_db.close()

    tenant_locks: dict[int, threading.Lock] = {}
    tenant_locks_guard = threading.Lock()

    def _fake_tenant_lock(self: AdmissionControlService, *, tenant_id: int) -> None:
        with tenant_locks_guard:
            tenant_lock = tenant_locks.setdefault(tenant_id, threading.Lock())
        tenant_lock.acquire()
        self._db.info["_tenant_admission_lock"] = tenant_lock

    monkeypatch.setattr(AdmissionControlService, "_acquire_advisory_xact_lock", _fake_tenant_lock)

    results: dict[int, tuple[bool, str | None]] = {}
    errors: list[BaseException] = []
    start_event = threading.Event()

    def _worker(user_id: int) -> None:
        db = factory()
        service = AdmissionControlService(db)
        start_event.wait(timeout=2)
        try:
            result = service.admit_task(
                tenant_id=tenant_id,
                user_id=user_id,
                placement="local",
                write_task=lambda _selection: _delayed_seed_task(
                    db,
                    tenant_id=tenant_id,
                    user_id=user_id,
                ),
            )
            results[user_id] = (result.decision.allowed, result.decision.reason_code)
        except BaseException as exc:  # pragma: no cover - surfaced via assertion below
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
    allowed_count = sum(1 for allowed, _reason in results.values() if allowed)
    assert allowed_count == 1
    denied_reasons = [reason for allowed, reason in results.values() if not allowed]
    assert denied_reasons == ["TENANT_QUOTA_EXCEEDED"]

    verify_db = factory()
    assert verify_db.query(Task).filter(Task.tenant_id == tenant_id).count() == 1
    verify_db.close()


def _delayed_seed_task(db: Session, *, tenant_id: int, user_id: int) -> Task:
    """Slow write callback to widen the count/write race window in parallel tests."""
    time.sleep(0.1)
    return _seed_task(
        db,
        tenant_id=tenant_id,
        user_id=user_id,
        status=TaskStatus.CREATED.value,
    )
