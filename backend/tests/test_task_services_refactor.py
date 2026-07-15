"""Unit tests for task service extractions from the task router refactor.

Responsibilities:
- Validate lifecycle/runtime/cleanup/interrupt service behavior in isolation.
- Lock refactor behavior for key orchestration and error-mapping paths.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.domain.task_lifecycle import TaskStateTransition, TaskStatus
from backend.schemas.vpn import TaskCreateVPN
from backend.services.langgraph_chat.checkpoint.interrupt_ticket_service import (
    InterruptTicketClaimConflictError,
)
from backend.services.runtime_provider.contracts import (
    RuntimeActorType,
    RuntimeCallScope,
    RuntimeOperationRequest,
    RuntimeOperationStatus,
    RuntimePlacementMode,
    build_runtime_result,
)
from backend.services.task.cleanup_service import TaskCleanupService
from backend.services.task.interrupt_service import TaskInterruptService
from backend.services.task.lifecycle_service import TaskLifecycleService
from backend.services.task.runtime_service import (
    PRODUCT_LOCAL_RUNTIME_PLACEMENT_REJECTED,
    TaskRuntimeService,
)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return 0 if self._value is None else self._value

    def one_or_none(self):
        return self._value


class _LifecycleDB:
    def __init__(self, existing_task=None):
        self.existing_task = existing_task
        self.added = []
        self.commits = 0
        self.refresh_calls = 0

    def execute(self, _query):
        return _ScalarResult(self.existing_task)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        return None

    def commit(self):
        self.commits += 1

    def refresh(self, obj):
        self.refresh_calls += 1
        if getattr(obj, "id", None) is None:
            obj.id = 101

    def rollback(self):
        pass

    def get_bind(self):
        return None


class _TenantScopedLifecycleDB(_LifecycleDB):
    def __init__(self, existing_tasks_by_scope):
        super().__init__(existing_task=None)
        self._existing_tasks_by_scope = existing_tasks_by_scope

    def execute(self, query):
        params = query.compile().params
        key = (
            int(params.get("user_id_1", 0)),
            int(params.get("tenant_id_1", 0)),
            str(params.get("name_1", "")),
        )
        if params.get("name_1") is not None:
            return _ScalarResult(self._existing_tasks_by_scope.get(key))
        return _ScalarResult(None)


class _FakeRuntimeProvider:
    def __init__(
        self,
        *,
        accepted: bool = True,
        status: RuntimeOperationStatus = RuntimeOperationStatus.SUCCEEDED,
        vpn_config_accepted: bool | None = None,
        vpn_retry_accepted: bool | None = None,
    ):
        self.accepted = accepted
        self.status = status
        self.vpn_config_accepted = accepted if vpn_config_accepted is None else vpn_config_accepted
        self.vpn_retry_accepted = accepted if vpn_retry_accepted is None else vpn_retry_accepted
        self.provider_name = "fake_provider"
        self.materialize_requests: list[RuntimeOperationRequest] = []
        self.vpn_config_requests: list[RuntimeOperationRequest] = []
        self.vpn_retry_requests: list[RuntimeOperationRequest] = []
        self.provision_requests: list[RuntimeOperationRequest] = []

    async def materialize_runtime_workspace(self, request: RuntimeOperationRequest):
        self.materialize_requests.append(request)
        return build_runtime_result(
            request,
            accepted=self.accepted,
            provider=self.provider_name,
            status=self.status,
            error_code=None if self.accepted else "materialize_failed",
            error_message=None if self.accepted else "materialize failed",
        )

    async def materialize_vpn_config(self, request: RuntimeOperationRequest):
        self.vpn_config_requests.append(request)
        return build_runtime_result(
            request,
            accepted=self.vpn_config_accepted,
            provider=self.provider_name,
            status=self.status if self.vpn_config_accepted else RuntimeOperationStatus.FAILED,
            error_code=None if self.vpn_config_accepted else "vpn_materialize_failed",
            error_message=None if self.vpn_config_accepted else "vpn materialize failed",
        )

    async def retry_vpn_connection(self, request: RuntimeOperationRequest):
        self.vpn_retry_requests.append(request)
        return build_runtime_result(
            request,
            accepted=self.vpn_retry_accepted,
            provider=self.provider_name,
            status=self.status if self.vpn_retry_accepted else RuntimeOperationStatus.FAILED,
            error_code=None if self.vpn_retry_accepted else "vpn_retry_failed",
            error_message=None if self.vpn_retry_accepted else "vpn retry failed",
        )

    async def provision_task_runtime(self, request: RuntimeOperationRequest):
        self.provision_requests.append(request)
        return build_runtime_result(
            request,
            accepted=self.accepted,
            provider=self.provider_name,
            status=self.status,
            error_code=None if self.accepted else "provision_failed",
            error_message=None if self.accepted else "runtime failed",
        )


class _FakeRuntimeProviderRegistry:
    def __init__(self, provider: _FakeRuntimeProvider):
        self.provider = provider

    def get_provider_for_task(self, _task):
        return self.provider

    def get_provider(self, **_kwargs):
        return self.provider


def _owned_task(
    task_id: int,
    user_id: int,
    *,
    runtime_placement_mode: str = "local",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        user_id=user_id,
        tenant_id=1,
        graph_thread_id="a" * 32,
        workspace_id=f"task-{task_id}",
        runtime_placement_mode=runtime_placement_mode,
        runner_id=None,
        execution_site_id=None,
    )


def _created_task_from(db: _LifecycleDB):
    return next(item for item in db.added if hasattr(item, "runtime_placement_mode"))


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_service_rejects_empty_name() -> None:
    db = _LifecycleDB()
    service = TaskLifecycleService(db)

    with pytest.raises(HTTPException) as exc:
        service.create_task(TaskCreateVPN(name="   "), user_id=1)

    assert exc.value.status_code == 400
    assert exc.value.detail == "Task name cannot be empty"


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_service_rejects_duplicate_active_task() -> None:
    db = _LifecycleDB(existing_task=SimpleNamespace(id=55))
    service = TaskLifecycleService(db)

    with pytest.raises(HTTPException) as exc:
        service.create_task(
            TaskCreateVPN(name="dup-task"),
            user_id=1,
            tenant_context=SimpleNamespace(tenant_id=1, user_id=1, role="owner"),
        )

    assert exc.value.status_code == 409
    assert "already active" in exc.value.detail


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_service_duplicate_task_conflict_is_scoped_to_active_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _TenantScopedLifecycleDB(
        existing_tasks_by_scope={
            (7, 701, "dup-task"): SimpleNamespace(id=55),
        }
    )
    service = TaskLifecycleService(db)

    class FakeEngagementService:
        def __init__(self, _db):
            pass

        def resolve_for_task_creation(self, **_kwargs):
            return None

    monkeypatch.setattr("backend.services.task.lifecycle_service.EngagementService", FakeEngagementService)
    monkeypatch.setattr(service, "materialize_runtime_workspace_for_task", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_queue_and_start_background_init", lambda *_args, **_kwargs: None)

    created = service.create_task(
        TaskCreateVPN(name="dup-task"),
        user_id=7,
        tenant_context=SimpleNamespace(tenant_id=702, user_id=7, role="owner"),
        runtime_call_scope=RuntimeCallScope.TEST,
        requested_runtime_placement_mode=RuntimePlacementMode.LOCAL,
    )

    assert created.id == 101
    assert created.tenant_id == 702
    assert created.name == "dup-task"


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_service_duplicate_task_conflict_remains_within_same_tenant() -> None:
    db = _TenantScopedLifecycleDB(
        existing_tasks_by_scope={
            (7, 701, "dup-task"): SimpleNamespace(id=55),
        }
    )
    service = TaskLifecycleService(db)

    with pytest.raises(HTTPException) as exc:
        service.create_task(
            TaskCreateVPN(name="dup-task"),
            user_id=7,
            tenant_context=SimpleNamespace(tenant_id=701, user_id=7, role="owner"),
        )

    assert exc.value.status_code == 409
    assert "already active" in exc.value.detail


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_service_creates_task_and_delegates_bootstrap_and_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _LifecycleDB()
    service = TaskLifecycleService(db)
    calls: dict[str, tuple] = {}

    def fake_materialize(*, task, user_id, task_data, actor_type, runtime_call_scope):
        calls["materialize"] = (
            task.id,
            task_data.name,
            user_id,
            actor_type,
            task.runtime_placement_mode,
            runtime_call_scope,
        )

    def fake_queue(task_id, user_id, task_log_id, **_kwargs):
        calls["queue"] = (task_id, user_id, task_log_id)

    class FakeTenantContextService:
        def __init__(self, _db):
            pass

        def resolve_for_user(self, *, user_id: int):
            return SimpleNamespace(tenant_id=1, user_id=user_id, role="owner")

    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.TenantContextService",
        FakeTenantContextService,
    )
    monkeypatch.setattr(service, "materialize_runtime_workspace_for_task", fake_materialize)
    monkeypatch.setattr(service, "_queue_and_start_background_init", fake_queue)

    created = service.create_task(
        TaskCreateVPN(name="  task-a  ", description="  desc  ", scope="net"),
        user_id=7,
        runtime_call_scope=RuntimeCallScope.TEST,
        requested_runtime_placement_mode=RuntimePlacementMode.LOCAL,
    )

    assert created.id == 101
    assert getattr(created, "engagement", None) is not None
    assert created.name == "task-a"
    assert created.description == "desc"
    assert created.status == TaskStatus.CREATED.value
    assert created.runtime_placement_mode == "local"
    assert len(db.added) == 2
    assert calls["materialize"] == (
        101,
        "  task-a  ",
        7,
        RuntimeActorType.USER,
        "local",
        RuntimeCallScope.TEST,
    )
    assert calls["queue"] == (101, 7, 101)


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_service_persists_runner_default_runtime_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _LifecycleDB()
    service = TaskLifecycleService(db)
    calls: dict[str, tuple] = {}

    def fake_materialize(*, task, user_id, task_data, actor_type, runtime_call_scope):
        calls["materialize"] = (
            task.id,
            task_data.name,
            user_id,
            actor_type,
            task.runtime_placement_mode,
            runtime_call_scope,
        )

    def fake_queue(task_id, user_id, task_log_id, **_kwargs):
        calls["queue"] = (task_id, user_id, task_log_id)

    class FakeTenantContextService:
        def __init__(self, _db):
            pass

        def resolve_for_user(self, *, user_id: int):
            return SimpleNamespace(tenant_id=1, user_id=user_id, role="owner")

    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.TenantContextService",
        FakeTenantContextService,
    )

    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.resolve_product_runtime_policy",
        lambda: SimpleNamespace(
            profile="single_host",
            product_runtime_placement="runner",
            cloud_runner_control_enabled=True,
            runner_tool_command_enabled=True,
        ),
    )

    class FakeAdmissionControlService:
        def __init__(self, _db):
            pass

        def admit_task(self, *, tenant_id, user_id, placement, write_task):
            assert (tenant_id, user_id, placement) == (1, 9, "runner")
            created_task = write_task(SimpleNamespace(runner_id="runner-1", execution_site_id="site-1"))
            return SimpleNamespace(
                decision=SimpleNamespace(allowed=True, reason_code=None, message=None),
                task=created_task,
            )

    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.AdmissionControlService",
        FakeAdmissionControlService,
    )
    monkeypatch.setattr(service, "materialize_runtime_workspace_for_task", fake_materialize)
    monkeypatch.setattr(service, "_queue_and_start_background_init", fake_queue)

    created = service.create_task(TaskCreateVPN(name="runner-task", description="desc"), user_id=9)

    assert created.runtime_placement_mode == "runner"
    assert created.runner_id == "runner-1"
    assert created.execution_site_id == "site-1"
    assert calls["materialize"] == (
        101,
        "runner-task",
        9,
        RuntimeActorType.USER,
        "runner",
        RuntimeCallScope.PRODUCT_TASK,
    )
    assert calls["queue"] == (101, 9, 101)


@pytest.mark.execution_plane_non_dind_regression
@pytest.mark.parametrize("profile", ("single_host", "distributed"))
def test_lifecycle_service_fails_closed_when_product_profile_uses_local_runtime_default(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
) -> None:
    db = _LifecycleDB()
    service = TaskLifecycleService(db)

    class FakeTenantContextService:
        def __init__(self, _db):
            pass

        def resolve_for_user(self, *, user_id: int):
            return SimpleNamespace(tenant_id=1, user_id=user_id, role="owner")

    class FakeEngagementService:
        def __init__(self, _db):
            pass

        def resolve_for_task_creation(self, **_kwargs):
            return None

    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.TenantContextService",
        FakeTenantContextService,
    )
    monkeypatch.setattr("backend.services.task.lifecycle_service.EngagementService", FakeEngagementService)
    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.resolve_product_runtime_policy",
        lambda: SimpleNamespace(
            profile=profile,
            product_runtime_placement="local",
            cloud_runner_control_enabled=True,
            runner_tool_command_enabled=True,
        ),
    )
    monkeypatch.setattr(
        service,
        "materialize_runtime_workspace_for_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("materialize should not run")),
    )

    with pytest.raises(HTTPException) as exc:
        service.create_task(TaskCreateVPN(name="misconfigured-profile-task"), user_id=17)

    assert exc.value.status_code == 500
    assert exc.value.detail == {
        "reason_code": "PRODUCT_LOCAL_PLACEMENT_FORBIDDEN",
        "message": "Product task execution must use runner placement.",
        "scope": "product_task",
    }


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_service_allows_explicit_local_creation_only_for_test_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _LifecycleDB()
    service = TaskLifecycleService(db)
    calls: dict[str, str] = {}

    class FakeTenantContextService:
        def __init__(self, _db):
            pass

        def resolve_for_user(self, *, user_id: int):
            return SimpleNamespace(tenant_id=1, user_id=user_id, role="owner")

    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.TenantContextService",
        FakeTenantContextService,
    )
    monkeypatch.setattr(service, "materialize_runtime_workspace_for_task", lambda **_kwargs: None)
    monkeypatch.setattr(service, "_queue_and_start_background_init", lambda *_args, **_kwargs: True)

    created = service.create_task(
        TaskCreateVPN(name="test-local-task"),
        user_id=17,
        runtime_call_scope=RuntimeCallScope.TEST,
        requested_runtime_placement_mode=RuntimePlacementMode.LOCAL,
    )
    calls["test_scope"] = created.runtime_placement_mode

    with pytest.raises(HTTPException) as exc:
        service.create_task(
            TaskCreateVPN(name="product-local-task"),
            user_id=17,
            requested_runtime_placement_mode=RuntimePlacementMode.LOCAL,
        )

    assert calls == {"test_scope": "local"}
    assert exc.value.detail["reason_code"] == "PRODUCT_LOCAL_PLACEMENT_FORBIDDEN"


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_service_uses_test_scoped_local_placement_in_deterministic_e2e(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _LifecycleDB()
    service = TaskLifecycleService(db)

    class FakeTenantContextService:
        def __init__(self, _db):
            pass

        def resolve_for_user(self, *, user_id: int):
            return SimpleNamespace(tenant_id=1, user_id=user_id, role="owner")

    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.TenantContextService",
        FakeTenantContextService,
    )
    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.E2E_DETERMINISTIC_MODE",
        True,
    )
    monkeypatch.setattr(service, "_complete_deterministic_e2e_bootstrap", lambda **_kwargs: None)

    created = service.create_task(
        TaskCreateVPN(name="deterministic-e2e-local-task"),
        user_id=17,
    )

    assert created.runtime_placement_mode == RuntimePlacementMode.LOCAL.value


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_service_rejects_runner_assignment_before_task_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _LifecycleDB()
    service = TaskLifecycleService(db)

    class FakeTenantContextService:
        def __init__(self, _db):
            pass

        def resolve_for_user(self, *, user_id: int):
            return SimpleNamespace(tenant_id=1, user_id=user_id, role="owner")

    class FakeEngagementService:
        def __init__(self, _db):
            pass

        def resolve_for_task_creation(self, **_kwargs):
            return None

    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.TenantContextService",
        FakeTenantContextService,
    )
    monkeypatch.setattr("backend.services.task.lifecycle_service.EngagementService", FakeEngagementService)
    monkeypatch.setattr(service, "_resolve_task_create_runtime_placement_mode", lambda **_kwargs: "runner")
    class FakeAdmissionControlService:
        def __init__(self, _db):
            pass

        def admit_task(self, *, tenant_id, user_id, placement, write_task):
            _ = (tenant_id, user_id, placement, write_task)
            return SimpleNamespace(
                decision=SimpleNamespace(
                    allowed=False,
                    reason_code="RUNNER_CAPACITY_EXHAUSTED",
                    message="No eligible runner is available for this task",
                ),
                task=None,
            )

    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.AdmissionControlService",
        FakeAdmissionControlService,
    )
    monkeypatch.setattr(
        service,
        "materialize_runtime_workspace_for_task",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("materialize should not run")),
    )

    with pytest.raises(HTTPException) as exc:
        service.create_task(TaskCreateVPN(name="runner-capacity-full"), user_id=9)

    assert exc.value.status_code == 409
    assert db.added == []
    assert db.commits == 0


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_bootstrap_materializes_workspace_through_runtime_provider() -> None:
    db = _LifecycleDB()
    provider = _FakeRuntimeProvider()
    service = TaskLifecycleService(
        db,
        runtime_provider_registry=_FakeRuntimeProviderRegistry(provider),
    )
    task = SimpleNamespace(
        id=101,
        tenant_id=1,
        user_id=7,
        graph_thread_id="a" * 32,
        workspace_id="task-101",
        runtime_placement_mode="local",
        runner_id=None,
        execution_site_id=None,
        name="task-a",
        description="desc",
        scope="network",
        timeout_seconds=3600,
        max_retries=3,
        priority=1,
    )
    task_data = TaskCreateVPN(name="task-a", scope="network")

    service.materialize_runtime_workspace_for_task(
        task=task,
        task_data=task_data,
        user_id=7,
        runtime_call_scope=RuntimeCallScope.TEST,
    )

    assert len(provider.materialize_requests) == 1
    request = provider.materialize_requests[0]
    assert request.operation == "materialize_runtime_workspace"
    assert request.tenant_id == 1
    assert request.task_id == 101
    assert request.runtime_placement_mode == RuntimePlacementMode.LOCAL
    assert request.payload["config_data"]["task_name"] == "task-a"
    assert request.payload["scope_content"] == "network"
    assert "db_session" not in request.metadata
    json.dumps(request.metadata)


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_materialization_failure_marks_task_failed_and_skips_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _LifecycleDB()
    service = TaskLifecycleService(db)
    calls: dict[str, list] = {"state": [], "queue": []}

    def fake_materialize(**_kwargs):
        db.existing_task = _created_task_from(db)
        raise RuntimeError("provider workspace unavailable")

    def fake_queue(*args, **_kwargs):
        calls["queue"].append(args)

    class FakeTenantContextService:
        def __init__(self, _db):
            pass

        def resolve_for_user(self, *, user_id: int):
            return SimpleNamespace(tenant_id=1, user_id=user_id, role="owner")

    class FakeTaskStateService:
        def __init__(self, _db):
            pass

        def change_task_status(self, **kwargs):
            calls["state"].append(kwargs)
            db.existing_task.status = kwargs["new_status"]
            return True, "ok", None

    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.TenantContextService",
        FakeTenantContextService,
    )
    monkeypatch.setattr("backend.services.task.lifecycle_service.TaskStateService", FakeTaskStateService)
    monkeypatch.setattr(service, "materialize_runtime_workspace_for_task", fake_materialize)
    monkeypatch.setattr(service, "_queue_and_start_background_init", fake_queue)

    created = service.create_task(
        TaskCreateVPN(name="task-a"),
        user_id=7,
        runtime_call_scope=RuntimeCallScope.TEST,
        requested_runtime_placement_mode=RuntimePlacementMode.LOCAL,
    )

    task = _created_task_from(db)
    assert created is task
    assert calls["queue"] == []
    assert calls["state"][0]["new_status"] == TaskStatus.FAILED.value
    assert "provider workspace unavailable" in calls["state"][0]["reason"]
    assert task.status == TaskStatus.FAILED.value
    assert task.error_message == calls["state"][0]["reason"]
    assert task.failure_reason == "runtime_workspace_materialization_failed"


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_queue_failure_marks_durable_task_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _LifecycleDB()
    service = TaskLifecycleService(db)
    calls: dict[str, list] = {"state": []}

    class FakeTenantContextService:
        def __init__(self, _db):
            pass

        def resolve_for_user(self, *, user_id: int):
            return SimpleNamespace(tenant_id=1, user_id=user_id, role="owner")

    class FakeTaskStateService:
        def __init__(self, _db):
            pass

        def change_task_status(self, **kwargs):
            calls["state"].append(kwargs)
            if kwargs["new_status"] == TaskStatus.QUEUED.value:
                return False, "queue transition blocked", None
            task = _created_task_from(db)
            db.existing_task = task
            task.status = kwargs["new_status"]
            return True, "ok", None

    monkeypatch.setattr(
        "backend.services.task.lifecycle_service.TenantContextService",
        FakeTenantContextService,
    )
    monkeypatch.setattr("backend.services.task.lifecycle_service.TaskStateService", FakeTaskStateService)
    monkeypatch.setattr(
        service,
        "materialize_runtime_workspace_for_task",
        lambda **_kwargs: build_runtime_result(
            RuntimeOperationRequest(
                tenant_id=1,
                task_id=101,
                user_id=7,
                actor_type=RuntimeActorType.USER,
                actor_id="7",
                runtime_placement_mode=RuntimePlacementMode.LOCAL,
                workspace_id="task-101",
                operation="materialize_runtime_workspace",
            ),
            accepted=True,
            provider="fake_provider",
            status=RuntimeOperationStatus.SUCCEEDED,
        ),
    )

    created = service.create_task(
        TaskCreateVPN(name="task-a"),
        user_id=7,
        runtime_call_scope=RuntimeCallScope.TEST,
        requested_runtime_placement_mode=RuntimePlacementMode.LOCAL,
    )

    assert created is _created_task_from(db)
    assert [call["new_status"] for call in calls["state"]] == [
        TaskStatus.QUEUED.value,
        TaskStatus.FAILED.value,
    ]
    assert created.status == TaskStatus.FAILED.value
    assert created.failure_reason == "task_queueing_failed"
    assert "queue transition blocked" in created.error_message


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_restart_materialization_reuses_persisted_vpn_config() -> None:
    db = _LifecycleDB()
    provider = _FakeRuntimeProvider()
    service = TaskLifecycleService(
        db,
        runtime_provider_registry=_FakeRuntimeProviderRegistry(provider),
    )
    task = SimpleNamespace(
        id=101,
        tenant_id=1,
        user_id=7,
        graph_thread_id="a" * 32,
        workspace_id="task-101",
        runtime_placement_mode="local",
        runner_id=None,
        execution_site_id=None,
        name="task-a",
        description="desc",
        scope="network",
        timeout_seconds=3600,
        max_retries=3,
        priority=1,
        vpn_enabled=True,
        vpn_provider="custom",
        vpn_config_data="Y2xpZW50CnZwbgo=",
    )

    asyncio.run(
        service.materialize_task_vpn_config_async(
            task=task,
            user_id=7,
            runtime_call_scope=RuntimeCallScope.TEST,
        )
    )

    assert len(provider.vpn_config_requests) == 1
    vpn_request = provider.vpn_config_requests[0]
    assert vpn_request.operation == "materialize_vpn_config"
    vpn_config = vpn_request.payload["vpn_config"]
    assert vpn_config.provider == "custom"
    assert vpn_config.config_data == "client\nvpn"
    assert "db_session" not in vpn_request.metadata
    json.dumps(vpn_request.metadata)
    assert len(provider.vpn_retry_requests) == 1
    retry_request = provider.vpn_retry_requests[0]
    assert retry_request.operation == "retry_vpn_connection"
    assert retry_request.payload == {"reason": "vpn_config_materialized"}
    assert "db_session" not in retry_request.metadata
    json.dumps(retry_request.metadata)


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_restart_materialization_records_failed_vpn_retry_without_raising() -> None:
    db = _LifecycleDB()
    provider = _FakeRuntimeProvider(vpn_retry_accepted=False)
    service = TaskLifecycleService(
        db,
        runtime_provider_registry=_FakeRuntimeProviderRegistry(provider),
    )
    task = SimpleNamespace(
        id=101,
        tenant_id=1,
        user_id=7,
        graph_thread_id="a" * 32,
        workspace_id="task-101",
        runtime_placement_mode="local",
        runner_id=None,
        execution_site_id=None,
        name="task-a",
        description="desc",
        scope="network",
        timeout_seconds=3600,
        max_retries=3,
        priority=1,
        vpn_enabled=True,
        vpn_provider="custom",
        vpn_config_data="Y2xpZW50CnZwbgo=",
        vpn_connection_status="configured",
        vpn_error_message=None,
    )

    asyncio.run(
        service.materialize_task_vpn_config_async(
            task=task,
            user_id=7,
            runtime_call_scope=RuntimeCallScope.TEST,
        )
    )

    assert len(provider.vpn_config_requests) == 1
    assert len(provider.vpn_retry_requests) == 1
    assert task.vpn_connection_status == "failed"
    assert task.vpn_error_message is not None
    assert "VPN connection retry failed" in task.vpn_error_message
    assert "vpn_retry_failed" in task.vpn_error_message
    assert db.commits == 1


@pytest.mark.execution_plane_non_dind_regression
def test_lifecycle_restart_materialization_records_missing_vpn_config_without_raising() -> None:
    db = _LifecycleDB()
    provider = _FakeRuntimeProvider()
    service = TaskLifecycleService(
        db,
        runtime_provider_registry=_FakeRuntimeProviderRegistry(provider),
    )
    task = SimpleNamespace(
        id=101,
        tenant_id=1,
        user_id=7,
        workspace_id="task-101",
        runtime_placement_mode="local",
        runner_id=None,
        execution_site_id=None,
        name="task-a",
        description="desc",
        scope="network",
        timeout_seconds=3600,
        max_retries=3,
        priority=1,
        vpn_enabled=True,
        vpn_provider="custom",
        vpn_config_data=None,
        vpn_connection_status="configured",
        vpn_error_message=None,
    )

    asyncio.run(service.materialize_task_vpn_config_async(task=task, user_id=7))

    assert provider.vpn_config_requests == []
    assert provider.vpn_retry_requests == []
    assert task.vpn_connection_status == "failed"
    assert task.vpn_error_message is not None
    assert "VPN enabled" in task.vpn_error_message
    assert db.commits == 1


class _ProvisionDB:
    def __init__(self, task):
        self.task = task
        self.commits = 0

    def execute(self, _query):
        return _ScalarResult(self.task)

    def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_lifecycle_initialization_uses_provider_success_path() -> None:
    task = _owned_task(task_id=15, user_id=3)
    provider = _FakeRuntimeProvider(accepted=True, status=RuntimeOperationStatus.RUNNING)
    db = _ProvisionDB(task)
    service = TaskLifecycleService(
        db,
        runtime_provider_registry=_FakeRuntimeProviderRegistry(provider),
    )

    class _StateService:
        def __init__(self):
            self.transitions = []

        def change_task_status(self, **kwargs):
            self.transitions.append(kwargs["new_status"])
            return True, "ok", None

    state_service = _StateService()

    ok = await service._start_unified_container_initialization(
        task_id=15,
        user_id=3,
        state_service=state_service,
        db=db,
        runtime_call_scope=RuntimeCallScope.TEST,
    )

    assert ok is True
    assert state_service.transitions == [TaskStatus.STARTING.value, TaskStatus.RUNNING.value]
    assert len(provider.provision_requests) == 1
    assert provider.provision_requests[0].operation == "provision_task_runtime"
    assert provider.provision_requests[0].runtime_placement_mode == RuntimePlacementMode.LOCAL


@pytest.mark.asyncio
async def test_lifecycle_initialization_does_not_start_vpn_when_running_transition_fails() -> None:
    task = _owned_task(task_id=16, user_id=3)
    task.vpn_enabled = True
    task.vpn_provider = "custom"
    task.vpn_config_data = "Y2xpZW50CnZwbgo="
    provider = _FakeRuntimeProvider(accepted=True, status=RuntimeOperationStatus.RUNNING)
    db = _ProvisionDB(task)
    service = TaskLifecycleService(
        db,
        runtime_provider_registry=_FakeRuntimeProviderRegistry(provider),
    )

    class _StateService:
        def __init__(self):
            self.transitions = []

        def change_task_status(self, **kwargs):
            transition = kwargs["new_status"]
            self.transitions.append(transition)
            if transition == TaskStatus.RUNNING.value:
                return False, "running transition rejected", None
            return True, "ok", None

    state_service = _StateService()

    ok = await service._start_unified_container_initialization(
        task_id=16,
        user_id=3,
        state_service=state_service,
        db=db,
        runtime_call_scope=RuntimeCallScope.TEST,
    )

    assert ok is False
    assert state_service.transitions == [
        TaskStatus.STARTING.value,
        TaskStatus.RUNNING.value,
        TaskStatus.FAILED.value,
    ]
    assert provider.vpn_config_requests == []
    assert provider.vpn_retry_requests == []


@pytest.mark.asyncio
async def test_lifecycle_initialization_uses_provider_failure_path() -> None:
    task = _owned_task(task_id=15, user_id=3)
    provider = _FakeRuntimeProvider(accepted=False, status=RuntimeOperationStatus.FAILED)
    db = _ProvisionDB(task)
    service = TaskLifecycleService(
        db,
        runtime_provider_registry=_FakeRuntimeProviderRegistry(provider),
    )

    class _StateService:
        def __init__(self):
            self.transitions = []
            self.reasons = []

        def change_task_status(self, **kwargs):
            self.transitions.append(kwargs["new_status"])
            self.reasons.append(kwargs.get("reason"))
            return True, "ok", None

    state_service = _StateService()

    ok = await service._start_unified_container_initialization(
        task_id=15,
        user_id=3,
        state_service=state_service,
        db=db,
        runtime_call_scope=RuntimeCallScope.TEST,
    )

    assert ok is False
    assert state_service.transitions == [TaskStatus.STARTING.value, TaskStatus.FAILED.value]
    assert len(provider.provision_requests) == 1
    assert state_service.reasons[-1] is not None
    assert task.error_message == state_service.reasons[-1]
    assert task.failure_reason == "provision_failed"


@pytest.mark.asyncio
async def test_lifecycle_initialization_runner_mode_keeps_state_transitions_management_owned() -> None:
    task = _owned_task(task_id=19, user_id=3, runtime_placement_mode="runner")
    provider = _FakeRuntimeProvider(accepted=True, status=RuntimeOperationStatus.ACCEPTED)
    provider.provider_name = "managed_runner"
    db = _ProvisionDB(task)
    service = TaskLifecycleService(
        db,
        runtime_provider_registry=_FakeRuntimeProviderRegistry(provider),
    )

    class _StateService:
        def __init__(self):
            self.transitions = []
            self.reasons = []

        def change_task_status(self, **kwargs):
            self.transitions.append(kwargs["new_status"])
            self.reasons.append(kwargs.get("reason"))
            return True, "ok", None

    state_service = _StateService()

    async def _runner_control_probe_result(request: RuntimeOperationRequest):
        provider.provision_requests.append(request)
        return build_runtime_result(
            request,
            accepted=True,
            provider="managed_runner",
            status=RuntimeOperationStatus.ACCEPTED,
            metadata={"protocol_domain": "runner_control"},
        )

    provider.provision_task_runtime = _runner_control_probe_result

    ok = await service._start_unified_container_initialization(
        task_id=19,
        user_id=3,
        state_service=state_service,
        db=db,
    )

    assert ok is False
    assert state_service.transitions == [TaskStatus.STARTING.value, TaskStatus.FAILED.value]
    assert len(provider.provision_requests) == 1
    assert provider.provision_requests[0].runtime_placement_mode == RuntimePlacementMode.RUNNER
    assert task.failure_reason == "RUNNER_REMOTE_OPERATION_DEFERRED"
    assert "deferred (runner_control)" in str(state_service.reasons[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pending_status",
    [RuntimeOperationStatus.ACCEPTED, RuntimeOperationStatus.RUNNING],
)
async def test_lifecycle_initialization_runner_mode_pending_keeps_starting_state(
    pending_status: RuntimeOperationStatus,
) -> None:
    task = _owned_task(task_id=20, user_id=3, runtime_placement_mode="runner")
    provider = _FakeRuntimeProvider(accepted=True, status=pending_status)
    provider.provider_name = "managed_runner"
    db = _ProvisionDB(task)
    service = TaskLifecycleService(
        db,
        runtime_provider_registry=_FakeRuntimeProviderRegistry(provider),
    )

    class _StateService:
        def __init__(self):
            self.transitions = []
            self.reasons = []

        def change_task_status(self, **kwargs):
            self.transitions.append(kwargs["new_status"])
            self.reasons.append(kwargs.get("reason"))
            return True, "ok", None

    state_service = _StateService()

    ok = await service._start_unified_container_initialization(
        task_id=20,
        user_id=3,
        state_service=state_service,
        db=db,
    )

    assert ok is True
    assert state_service.transitions == [TaskStatus.STARTING.value]
    assert len(provider.provision_requests) == 1
    assert provider.provision_requests[0].runtime_placement_mode == RuntimePlacementMode.RUNNER


class _RuntimeDB:
    def __init__(self):
        self.refresh_count = 0

    def refresh(self, _obj):
        self.refresh_count += 1


class _FakeRuntimeAdmissionControlService:
    def __init__(self, _db):
        pass

    def admit_task(self, *, tenant_id, user_id, placement, write_task):
        del tenant_id, user_id
        runner_selection = None
        if placement == RuntimePlacementMode.RUNNER.value:
            runner_selection = SimpleNamespace(runner_id="runner-1", execution_site_id="site-1")
        task = write_task(runner_selection)
        return SimpleNamespace(
            decision=SimpleNamespace(allowed=True, reason_code=None, message=None),
            task=task,
        )


@pytest.mark.asyncio
async def test_runtime_pause_returns_message_for_created_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    task = SimpleNamespace(status=TaskStatus.CREATED.value)

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: task,
    )

    result = await service.pause_task(task_id=1, user_id=1, tenant_id=1)
    assert result == {
        "message": "Task is not running yet. It's still in created state and hasn't been started."
    }


def _assert_product_local_runtime_rejection(exc: HTTPException, *, task_id: int) -> None:
    assert exc.status_code == 409
    assert exc.detail["reason_code"] == PRODUCT_LOCAL_RUNTIME_PLACEMENT_REJECTED
    assert exc.detail["task_id"] == task_id


@pytest.mark.asyncio
async def test_runtime_start_rejects_product_local_task_before_admission_or_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    task = SimpleNamespace(
        id=77,
        status=TaskStatus.CREATED.value,
        runtime_placement_mode="local",
        engagement=SimpleNamespace(status="active"),
    )

    class UnexpectedAdmissionControlService:
        def __init__(self, _db):
            raise AssertionError("product local start must not reach admission")

    class UnexpectedRuntimeOperations:
        async def run_authorized_task_operation(self, **_kwargs):
            raise AssertionError("product local start must not reach provider")

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_with_engagement_or_404",
        lambda db, task_id, user_id, tenant_id: task,
    )
    monkeypatch.setattr(
        "backend.services.task.runtime_service.AdmissionControlService",
        UnexpectedAdmissionControlService,
    )
    service._runtime_operations = UnexpectedRuntimeOperations()

    with pytest.raises(HTTPException) as exc:
        await service.start_task(task_id=77, user_id=9, tenant_id=1)

    _assert_product_local_runtime_rejection(exc.value, task_id=77)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "task_status", "lookup_path"),
    [
        ("pause_task", TaskStatus.RUNNING.value, "get_owned_task_or_404"),
        ("resume_task", TaskStatus.PAUSED.value, "get_owned_task_with_engagement_or_404"),
    ],
)
async def test_runtime_pause_and_resume_reject_product_local_task_before_provider(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    task_status: str,
    lookup_path: str,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    task = SimpleNamespace(
        id=78,
        status=task_status,
        runtime_placement_mode="local",
        engagement=SimpleNamespace(status="active"),
    )

    class UnexpectedRuntimeOperations:
        async def run_authorized_task_operation(self, **_kwargs):
            raise AssertionError("product local task must not reach provider")

    monkeypatch.setattr(
        f"backend.services.task.runtime_service.{lookup_path}",
        lambda db, task_id, user_id, tenant_id: task,
    )
    service._runtime_operations = UnexpectedRuntimeOperations()

    with pytest.raises(HTTPException) as exc:
        await getattr(service, method_name)(task_id=78, user_id=9, tenant_id=1)

    _assert_product_local_runtime_rejection(exc.value, task_id=78)


@pytest.mark.asyncio
async def test_runtime_pause_runner_pending_keeps_pausing_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    task = SimpleNamespace(status=TaskStatus.RUNNING.value, runtime_placement_mode="runner")
    transitions: list[str] = []

    class FakeStateService:
        def __init__(self, _db):
            pass

        def change_task_status(self, **kwargs):
            transitions.append(kwargs["new_status"])
            return True, "ok", None

    class FakeRuntimeOperations:
        async def run_user_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=True,
                provider="managed_runner",
                status=RuntimeOperationStatus.ACCEPTED,
            )

        async def run_authorized_task_operation(self, **kwargs):
            return await self.run_user_task_operation(**kwargs)

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: task,
    )
    monkeypatch.setattr("backend.services.task.runtime_service.TaskStateService", FakeStateService)
    service._runtime_operations = FakeRuntimeOperations()

    returned = await service.pause_task(task_id=40, user_id=2, tenant_id=1)

    assert returned.status == TaskStatus.RUNNING.value
    assert transitions == [TaskStatus.PAUSING.value]
    assert db.refresh_count == 1


@pytest.mark.asyncio
async def test_runtime_start_transitions_to_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    task = SimpleNamespace(
        id=1,
        status=TaskStatus.CREATED.value,
        engagement=SimpleNamespace(status="active"),
    )
    materialize_calls = []
    events = []

    class FakeStateService:
        def __init__(self, _db):
            self.db = SimpleNamespace(flush=lambda: None)

        def validate_operation(self, task_id, operation):
            return True, "Task can be started"

        def stage_task_status_change(self, **_kwargs):
            return True, "ok", None

        def change_task_status(self, **kwargs):
            events.append(f"status:{kwargs['new_status']}")
            return True, "ok", None

    class FakeRuntimeOperations:
        async def run_user_task_operation(self, **_kwargs):
            return SimpleNamespace(ok=True)

        async def run_authorized_task_operation(self, **kwargs):
            return await self.run_user_task_operation(**kwargs)

    class FakeLifecycleService:
        def __init__(self, _db):
            pass

        async def materialize_runtime_workspace_for_task_async(
            self,
            *,
            task,
            user_id,
            **_kwargs,
        ):
            materialize_calls.append((task.id, user_id))

        async def materialize_task_vpn_config_async(self, *, task, user_id, db=None, **_kwargs):
            del task, user_id, db
            events.append("vpn")

        @staticmethod
        def build_provision_payload(_task):
            return {}

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_with_engagement_or_404",
        lambda db, task_id, user_id, tenant_id: task,
    )
    monkeypatch.setattr("backend.services.task.runtime_service.TaskStateService", FakeStateService)
    monkeypatch.setattr("backend.services.task.runtime_service.TaskLifecycleService", FakeLifecycleService)
    monkeypatch.setattr(
        "backend.services.task.runtime_service.AdmissionControlService",
        _FakeRuntimeAdmissionControlService,
    )
    service._runtime_operations = FakeRuntimeOperations()

    returned = await service.start_task(task_id=1, user_id=9, tenant_id=1)
    assert returned.status == TaskStatus.CREATED.value
    assert materialize_calls == [(1, 9)]
    assert events == ["status:starting", "status:running", "vpn"]
    assert db.refresh_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pending_status",
    [RuntimeOperationStatus.ACCEPTED, RuntimeOperationStatus.RUNNING],
)
async def test_runtime_start_runner_pending_keeps_starting_state(
    monkeypatch: pytest.MonkeyPatch,
    pending_status: RuntimeOperationStatus,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    task = SimpleNamespace(
        id=22,
        status=TaskStatus.CREATED.value,
        runtime_placement_mode="runner",
        engagement=SimpleNamespace(status="active"),
    )
    transitions: list[str] = []
    vpn_materialize_calls: list[int] = []

    class FakeStateService:
        def __init__(self, _db):
            self.db = SimpleNamespace(flush=lambda: None)

        def validate_operation(self, task_id, operation):
            return True, "Task can be started"

        def stage_task_status_change(self, **kwargs):
            transitions.append(kwargs["new_status"])
            return True, "ok", None

        def change_task_status(self, **kwargs):
            transitions.append(kwargs["new_status"])
            return True, "ok", None

    class FakeRuntimeOperations:
        async def run_user_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=True,
                provider="managed_runner",
                status=pending_status,
                metadata={"protocol_domain": "remote_runtime"},
            )

        async def run_authorized_task_operation(self, **kwargs):
            return await self.run_user_task_operation(**kwargs)

    class FakeLifecycleService:
        def __init__(self, _db):
            pass

        async def materialize_runtime_workspace_for_task_async(self, *, task, user_id, **_kwargs):
            del task, user_id

        async def materialize_task_vpn_config_async(self, *, task, user_id, db=None):
            del user_id, db
            vpn_materialize_calls.append(task.id)

        @staticmethod
        def build_provision_payload(_task):
            return {}

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_with_engagement_or_404",
        lambda db, task_id, user_id, tenant_id: task,
    )
    monkeypatch.setattr("backend.services.task.runtime_service.TaskStateService", FakeStateService)
    monkeypatch.setattr("backend.services.task.runtime_service.TaskLifecycleService", FakeLifecycleService)
    monkeypatch.setattr(
        "backend.services.task.runtime_service.AdmissionControlService",
        _FakeRuntimeAdmissionControlService,
    )
    service._runtime_operations = FakeRuntimeOperations()

    returned = await service.start_task(task_id=22, user_id=9, tenant_id=1)

    assert returned.status == TaskStatus.CREATED.value
    assert transitions == [TaskStatus.QUEUED.value, TaskStatus.STARTING.value]
    assert vpn_materialize_calls == []
    assert db.refresh_count == 1


@pytest.mark.asyncio
async def test_runtime_start_runner_runner_control_probe_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    task = SimpleNamespace(
        id=2,
        status=TaskStatus.CREATED.value,
        runtime_placement_mode="runner",
        engagement=SimpleNamespace(status="active"),
    )
    transitions: list[str] = []

    class FakeStateService:
        def __init__(self, _db):
            self.db = SimpleNamespace(flush=lambda: None)

        def validate_operation(self, task_id, operation):
            return True, "Task can be started"

        def stage_task_status_change(self, **kwargs):
            transitions.append(kwargs["new_status"])
            return True, "ok", None

        def change_task_status(self, **kwargs):
            transitions.append(kwargs["new_status"])
            return True, "ok", None

    class FakeRuntimeOperations:
        async def run_user_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=True,
                provider="managed_runner",
                status=RuntimeOperationStatus.ACCEPTED,
                metadata={"protocol_domain": "runner_control"},
            )

        async def run_authorized_task_operation(self, **kwargs):
            return await self.run_user_task_operation(**kwargs)

    class FakeLifecycleService:
        def __init__(self, _db):
            pass

        async def materialize_runtime_workspace_for_task_async(self, *, task, user_id, **_kwargs):
            del task, user_id

        async def materialize_task_vpn_config_async(self, *, task, user_id, db=None):
            del task, user_id, db

        @staticmethod
        def build_provision_payload(_task):
            return {}

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_with_engagement_or_404",
        lambda db, task_id, user_id, tenant_id: task,
    )
    monkeypatch.setattr("backend.services.task.runtime_service.TaskStateService", FakeStateService)
    monkeypatch.setattr("backend.services.task.runtime_service.TaskLifecycleService", FakeLifecycleService)
    monkeypatch.setattr(
        "backend.services.task.runtime_service.AdmissionControlService",
        _FakeRuntimeAdmissionControlService,
    )
    service._runtime_operations = FakeRuntimeOperations()

    with pytest.raises(HTTPException) as exc:
        await service.start_task(task_id=2, user_id=9, tenant_id=1)

    assert exc.value.status_code == 409
    assert "deferred (runner_control)" in str(exc.value.detail)
    assert transitions == [TaskStatus.QUEUED.value, TaskStatus.STARTING.value, TaskStatus.FAILED.value]


@pytest.mark.asyncio
async def test_runtime_resume_raises_when_validation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    task = SimpleNamespace(status=TaskStatus.PAUSED.value)

    class FakeStateService:
        def __init__(self, _db):
            pass

        def validate_operation(self, task_id, op):
            assert task_id == 11
            assert op == "resume"
            return False, "Cannot resume now"

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_with_engagement_or_404",
        lambda db, task_id, user_id, tenant_id: SimpleNamespace(
            status=task.status,
            engagement=SimpleNamespace(status="active"),
        ),
    )
    monkeypatch.setattr("backend.services.task.runtime_service.TaskStateService", FakeStateService)

    with pytest.raises(HTTPException) as exc:
        await service.resume_task(task_id=11, user_id=3, tenant_id=1)

    assert exc.value.status_code == 400
    assert exc.value.detail == "Cannot resume now"


@pytest.mark.asyncio
async def test_runtime_resume_runner_pending_keeps_resuming_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    task = SimpleNamespace(
        status=TaskStatus.PAUSED.value,
        runtime_placement_mode="runner",
        engagement=SimpleNamespace(status="active"),
    )
    transitions: list[str] = []

    class FakeStateService:
        def __init__(self, _db):
            pass

        def validate_operation(self, task_id, operation):
            return True, "Task can be resumed"

        def change_task_status(self, **kwargs):
            transitions.append(kwargs["new_status"])
            return True, "ok", None

    class FakeRuntimeOperations:
        async def run_user_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=True,
                provider="managed_runner",
                status=RuntimeOperationStatus.ACCEPTED,
            )

        async def run_authorized_task_operation(self, **kwargs):
            return await self.run_user_task_operation(**kwargs)

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_with_engagement_or_404",
        lambda db, task_id, user_id, tenant_id: task,
    )
    monkeypatch.setattr("backend.services.task.runtime_service.TaskStateService", FakeStateService)
    service._runtime_operations = FakeRuntimeOperations()

    returned = await service.resume_task(task_id=41, user_id=2, tenant_id=1)

    assert returned.status == TaskStatus.PAUSED.value
    assert transitions == [TaskStatus.RESUMING.value]
    assert db.refresh_count == 1


@pytest.mark.asyncio
async def test_runtime_start_rejects_archived_engagement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_with_engagement_or_404",
        lambda db, task_id, user_id, tenant_id: SimpleNamespace(
            status=TaskStatus.CREATED.value,
            engagement=SimpleNamespace(status="archived"),
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await service.start_task(task_id=12, user_id=5, tenant_id=1)

    assert exc.value.status_code == 409
    assert "archived engagement" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_runtime_resume_rejects_archived_engagement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_with_engagement_or_404",
        lambda db, task_id, user_id, tenant_id: SimpleNamespace(
            status=TaskStatus.PAUSED.value,
            engagement=SimpleNamespace(status="archived"),
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await service.resume_task(task_id=13, user_id=5, tenant_id=1)

    assert exc.value.status_code == 409
    assert "archived engagement" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_runtime_stop_transitions_to_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    task = SimpleNamespace(status=TaskStatus.RUNNING.value)
    captured: dict[str, object] = {}

    class FakeStateService:
        def __init__(self, _db):
            pass

        def validate_operation(self, task_id, operation):
            assert task_id == 31
            assert operation == "stop"
            return True, "Task can be stopped"

        def change_task_status(self, **kwargs):
            captured.setdefault("status_changes", []).append(kwargs)
            return True, "ok", None

    class FakeRetirementService:
        async def retire_runtime(
            self,
            *,
            task_id: int,
            engagement_id: int | None,
            runtime_call_scope: RuntimeCallScope,
            user_id: int | None = None,
        ):
            assert task_id == 31
            assert engagement_id == 6
            assert user_id == 7
            assert runtime_call_scope is RuntimeCallScope.TEST
            return SimpleNamespace(success=True, message="retired")

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: SimpleNamespace(
            status=task.status,
            engagement_id=6,
            runtime_placement_mode="local",
        ),
    )
    monkeypatch.setattr("backend.services.task.runtime_service.TaskStateService", FakeStateService)
    monkeypatch.setattr("backend.services.task.runtime_service.TaskRetirementService", FakeRetirementService)

    returned = await service.stop_task(
        task_id=31,
        user_id=7,
        tenant_id=1,
        runtime_call_scope=RuntimeCallScope.TEST,
    )

    assert returned.status == TaskStatus.RUNNING.value
    assert [entry["new_status"] for entry in captured["status_changes"]] == [
        TaskStatus.STOPPING.value,
        TaskStatus.STOPPED.value,
    ]
    assert db.refresh_count == 1


@pytest.mark.asyncio
async def test_runtime_stop_rejects_product_local_task_before_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    task = SimpleNamespace(
        id=79,
        status=TaskStatus.RUNNING.value,
        runtime_placement_mode="local",
        engagement_id=6,
    )

    class UnexpectedStateService:
        def __init__(self, _db):
            raise AssertionError("product local stop must not validate or transition")

    class UnexpectedRetirementService:
        def __init__(self):
            raise AssertionError("product local stop must not retire local runtime")

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: task,
    )
    monkeypatch.setattr(
        "backend.services.task.runtime_service.TaskStateService",
        UnexpectedStateService,
    )
    monkeypatch.setattr(
        "backend.services.task.runtime_service.TaskRetirementService",
        UnexpectedRetirementService,
    )

    with pytest.raises(HTTPException) as exc:
        await service.stop_task(task_id=79, user_id=7, tenant_id=1)

    _assert_product_local_runtime_rejection(exc.value, task_id=79)


@pytest.mark.asyncio
async def test_runtime_stop_runner_pending_uses_stop_provider_not_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    task = SimpleNamespace(
        status=TaskStatus.RUNNING.value,
        runtime_placement_mode="runner",
        engagement_id=6,
    )
    captured: dict[str, object] = {}

    class FakeStateService:
        def __init__(self, _db):
            pass

        def validate_operation(self, task_id, operation):
            return True, "Task can be stopped"

        def change_task_status(self, **kwargs):
            captured.setdefault("status_changes", []).append(kwargs)
            return True, "ok", None

    class FakeRuntimeOperations:
        async def run_user_task_operation(self, **kwargs):
            captured["operation"] = kwargs["operation"]
            return SimpleNamespace(
                ok=True,
                provider="managed_runner",
                status=RuntimeOperationStatus.ACCEPTED,
            )

        async def run_authorized_task_operation(self, **kwargs):
            return await self.run_user_task_operation(**kwargs)

    class FakeRetirementService:
        async def retire_runtime(self, **_kwargs):
            raise AssertionError("Runner stop should not call retirement service")

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: task,
    )
    monkeypatch.setattr("backend.services.task.runtime_service.TaskStateService", FakeStateService)
    monkeypatch.setattr("backend.services.task.runtime_service.TaskRetirementService", FakeRetirementService)
    service._runtime_operations = FakeRuntimeOperations()

    returned = await service.stop_task(task_id=50, user_id=9, tenant_id=1)

    assert returned.status == TaskStatus.RUNNING.value
    assert captured["operation"] == "stop_task_runtime"
    assert [entry["new_status"] for entry in captured["status_changes"]] == [
        TaskStatus.STOPPING.value,
    ]
    assert db.refresh_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial_status",
    [TaskStatus.PAUSING.value, TaskStatus.RESUMING.value],
)
async def test_runtime_stop_allows_pausing_and_resuming_with_real_transition_validation(
    monkeypatch: pytest.MonkeyPatch,
    initial_status: str,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    task = SimpleNamespace(status=initial_status, engagement_id=12)
    captured: dict[str, object] = {}

    class TransitionValidatingStateService:
        def __init__(self, _db):
            pass

        def validate_operation(self, task_id, operation):
            assert task_id == 34
            assert operation == "stop"
            return True, "Task can be stopped"

        def change_task_status(self, **kwargs):
            is_valid, message = TaskStateTransition.validate_transition(task.status, kwargs["new_status"])
            if not is_valid:
                return False, message, None
            captured.setdefault("status_changes", []).append(kwargs)
            task.status = kwargs["new_status"]
            return True, "ok", None

    class FakeRetirementService:
        async def retire_runtime(self, *, task_id: int, engagement_id: int | None):
            assert task_id == 34
            assert engagement_id == 12
            return SimpleNamespace(success=True, message="retired")

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: task,
    )
    monkeypatch.setattr(
        "backend.services.task.runtime_service.TaskStateService",
        TransitionValidatingStateService,
    )
    monkeypatch.setattr("backend.services.task.runtime_service.TaskRetirementService", FakeRetirementService)

    returned = await service.stop_task(task_id=34, user_id=7, tenant_id=1)

    assert returned.status == TaskStatus.STOPPED.value
    assert [entry["new_status"] for entry in captured["status_changes"]] == [
        TaskStatus.STOPPING.value,
        TaskStatus.STOPPED.value,
    ]
    assert db.refresh_count == 1


@pytest.mark.asyncio
async def test_runtime_stop_moves_to_failed_when_retirement_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    captured: dict[str, object] = {}

    class FakeStateService:
        def __init__(self, _db):
            pass

        def validate_operation(self, task_id, operation):
            assert task_id == 32
            assert operation == "stop"
            return True, "Task can be stopped"

        def change_task_status(self, **kwargs):
            captured.setdefault("status_changes", []).append(kwargs)
            return True, "ok", None

    class FakeRetirementService:
        async def retire_runtime(self, *, task_id: int, engagement_id: int | None):
            assert task_id == 32
            assert engagement_id == 8
            return SimpleNamespace(success=False, message="workspace cleanup failed")

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: SimpleNamespace(
            status=TaskStatus.RUNNING.value,
            engagement_id=8,
        ),
    )
    monkeypatch.setattr("backend.services.task.runtime_service.TaskStateService", FakeStateService)
    monkeypatch.setattr("backend.services.task.runtime_service.TaskRetirementService", FakeRetirementService)

    with pytest.raises(HTTPException) as exc:
        await service.stop_task(task_id=32, user_id=7, tenant_id=1)

    assert exc.value.status_code == 500
    assert exc.value.detail == "workspace cleanup failed"
    assert [entry["new_status"] for entry in captured["status_changes"]] == [
        TaskStatus.STOPPING.value,
        TaskStatus.FAILED.value,
    ]


@pytest.mark.asyncio
async def test_runtime_stop_moves_to_failed_when_retirement_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    captured: dict[str, object] = {}

    class FakeStateService:
        def __init__(self, _db):
            pass

        def validate_operation(self, task_id, operation):
            assert task_id == 36
            assert operation == "stop"
            return True, "Task can be stopped"

        def change_task_status(self, **kwargs):
            captured.setdefault("status_changes", []).append(kwargs)
            return True, "ok", None

    class FakeRetirementService:
        async def retire_runtime(self, *, task_id: int, engagement_id: int | None):
            assert task_id == 36
            assert engagement_id == 11
            raise RuntimeError("docker API exploded")

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: SimpleNamespace(
            status=TaskStatus.RUNNING.value,
            engagement_id=11,
        ),
    )
    monkeypatch.setattr("backend.services.task.runtime_service.TaskStateService", FakeStateService)
    monkeypatch.setattr("backend.services.task.runtime_service.TaskRetirementService", FakeRetirementService)

    with pytest.raises(HTTPException) as exc:
        await service.stop_task(task_id=36, user_id=7, tenant_id=1)

    assert exc.value.status_code == 500
    assert "failed unexpectedly" in str(exc.value.detail)
    assert [entry["new_status"] for entry in captured["status_changes"]] == [
        TaskStatus.STOPPING.value,
        TaskStatus.FAILED.value,
    ]


@pytest.mark.asyncio
async def test_runtime_stop_handles_queued_without_invalid_stopping_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    captured: dict[str, object] = {}

    class FakeStateService:
        def __init__(self, _db):
            pass

        def validate_operation(self, task_id, operation):
            assert task_id == 33
            assert operation == "stop"
            return True, "Task can be stopped"

        def change_task_status(self, **kwargs):
            captured.setdefault("status_changes", []).append(kwargs)
            return True, "ok", None

    class FakeRetirementService:
        async def retire_runtime(self, *, task_id: int, engagement_id: int | None):
            assert task_id == 33
            assert engagement_id == 9
            return SimpleNamespace(success=True, message="runtime not found")

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: SimpleNamespace(
            status=TaskStatus.QUEUED.value,
            engagement_id=9,
        ),
    )
    monkeypatch.setattr("backend.services.task.runtime_service.TaskStateService", FakeStateService)
    monkeypatch.setattr("backend.services.task.runtime_service.TaskRetirementService", FakeRetirementService)

    returned = await service.stop_task(task_id=33, user_id=7, tenant_id=1)

    assert returned.status == TaskStatus.QUEUED.value
    assert [entry["new_status"] for entry in captured["status_changes"]] == [
        TaskStatus.STOPPED.value,
    ]
    assert db.refresh_count == 1


@pytest.mark.asyncio
async def test_runtime_stop_handles_starting_without_invalid_stopping_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _RuntimeDB()
    service = TaskRuntimeService(db)
    captured: dict[str, object] = {}

    class FakeStateService:
        def __init__(self, _db):
            pass

        def validate_operation(self, task_id, operation):
            assert task_id == 35
            assert operation == "stop"
            return True, "Task can be stopped"

        def change_task_status(self, **kwargs):
            captured.setdefault("status_changes", []).append(kwargs)
            return True, "ok", None

    class FakeRetirementService:
        async def retire_runtime(self, *, task_id: int, engagement_id: int | None):
            assert task_id == 35
            assert engagement_id == 10
            return SimpleNamespace(success=True, message="runtime not found")

    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: SimpleNamespace(
            status=TaskStatus.STARTING.value,
            engagement_id=10,
        ),
    )
    monkeypatch.setattr("backend.services.task.runtime_service.TaskStateService", FakeStateService)
    monkeypatch.setattr("backend.services.task.runtime_service.TaskRetirementService", FakeRetirementService)

    returned = await service.stop_task(task_id=35, user_id=7, tenant_id=1)

    assert returned.status == TaskStatus.STARTING.value
    assert [entry["new_status"] for entry in captured["status_changes"]] == [
        TaskStatus.STOPPED.value,
    ]
    assert db.refresh_count == 1


class _CleanupDB:
    def __init__(self):
        self.executed: list[tuple[str, dict | None]] = []
        self.committed = False
        self.rolled_back = False

    def execute(self, statement, params=None):
        sql = str(statement)
        self.executed.append((sql, params))
        if "DELETE FROM tasks" in sql:
            return SimpleNamespace(rowcount=1)
        return SimpleNamespace(rowcount=0)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


@pytest.mark.asyncio
async def test_cleanup_service_deletes_task_related_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _CleanupDB()
    captured: dict[str, object] = {}

    class FakeGraphCleanupService:
        async def cleanup_task_graph_state(self, **kwargs):
            captured["graph_cleanup"] = kwargs

    service = TaskCleanupService(db, graph_state_cleanup_service=FakeGraphCleanupService())

    monkeypatch.setattr(
        "backend.services.task.cleanup_service.get_task_in_tenant_or_404",
        lambda db, task_id, tenant_id: SimpleNamespace(
            id=task_id,
            user_id=99,
            engagement_id=5,
            tenant_id=tenant_id,
            graph_thread_id="a" * 32,
        ),
    )

    class FakeRetirementService:
        async def retire_runtime(self, *, task_id: int, engagement_id: int | None):
            captured["retire"] = {"task_id": task_id, "engagement_id": engagement_id}
            return SimpleNamespace(success=True, message="retired")

    monkeypatch.setattr("backend.services.task.cleanup_service.TaskRetirementService", FakeRetirementService)
    monkeypatch.setattr(
        service,
        "_enforce_delete_safety_preflight",
        lambda **_kwargs: None,
    )

    result = await service.delete_task(task_id=21, user_id=1, tenant_id=1)

    assert result == {"message": "Task and container deleted successfully"}
    assert db.committed is True
    assert any("DELETE FROM tasks" in sql for sql, _ in db.executed)
    assert captured["retire"] == {"task_id": 21, "engagement_id": 5}
    assert captured["graph_cleanup"] == {
        "task_id": 21,
        "graph_thread_id": "a" * 32,
    }


@pytest.mark.asyncio
async def test_cleanup_service_propagates_not_found_from_tenant_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _CleanupDB()
    service = TaskCleanupService(
        db,
        graph_state_cleanup_service=SimpleNamespace(
            cleanup_task_graph_state=lambda **_kwargs: asyncio.sleep(0)
        ),
    )

    def fake_lookup(db, task_id: int, tenant_id: int):
        raise HTTPException(status_code=404, detail="Task not found")

    monkeypatch.setattr("backend.services.task.cleanup_service.get_task_in_tenant_or_404", fake_lookup)

    with pytest.raises(HTTPException) as exc:
        await service.delete_task(task_id=99, user_id=1, tenant_id=1)

    assert exc.value.status_code == 404
    assert exc.value.detail == "Task not found"


@pytest.mark.asyncio
async def test_cleanup_service_blocks_when_delete_preflight_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _CleanupDB()
    service = TaskCleanupService(
        db,
        graph_state_cleanup_service=SimpleNamespace(
            cleanup_task_graph_state=lambda **_kwargs: asyncio.sleep(0)
        ),
    )

    monkeypatch.setattr(
        "backend.services.task.cleanup_service.get_task_in_tenant_or_404",
        lambda db, task_id, tenant_id: SimpleNamespace(
            id=task_id,
            user_id=77,
            engagement_id=7,
            tenant_id=tenant_id,
            graph_thread_id="a" * 32,
        ),
    )
    monkeypatch.setattr(
        service,
        "_enforce_delete_safety_preflight",
        lambda **_kwargs: (_ for _ in ()).throw(
            HTTPException(status_code=409, detail="durable evidence incomplete")
        ),
    )
    monkeypatch.setattr(
        "backend.services.task.cleanup_service.TaskRetirementService",
        lambda: SimpleNamespace(
            retire_runtime=lambda **_kwargs: asyncio.sleep(
                0,
                result=SimpleNamespace(success=True, message="retired"),
            )
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await service.delete_task(task_id=45, user_id=1, tenant_id=1)

    assert exc.value.status_code == 409
    assert "durable evidence incomplete" in str(exc.value.detail)
    assert db.committed is False
    assert not any("DELETE FROM tasks" in sql for sql, _ in db.executed)


@pytest.mark.asyncio
async def test_cleanup_service_fails_when_runtime_retirement_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _CleanupDB()
    service = TaskCleanupService(
        db,
        graph_state_cleanup_service=SimpleNamespace(
            cleanup_task_graph_state=lambda **_kwargs: asyncio.sleep(0)
        ),
    )

    monkeypatch.setattr(
        "backend.services.task.cleanup_service.get_task_in_tenant_or_404",
        lambda db, task_id, tenant_id: SimpleNamespace(
            id=task_id,
            user_id=77,
            engagement_id=7,
            tenant_id=tenant_id,
            graph_thread_id="a" * 32,
        ),
    )
    monkeypatch.setattr(
        service,
        "_enforce_delete_safety_preflight",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "backend.services.task.cleanup_service.TaskRetirementService",
        lambda: SimpleNamespace(
            retire_runtime=lambda **_kwargs: asyncio.sleep(
                0,
                result=SimpleNamespace(success=False, message="runtime teardown failed"),
            )
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await service.delete_task(task_id=45, user_id=1, tenant_id=1)

    assert exc.value.status_code == 500
    assert exc.value.detail == "runtime teardown failed"
    assert db.rolled_back is True
    assert not any("DELETE FROM tasks" in sql for sql, _ in db.executed)


def test_cleanup_delete_safety_preflight_raises_409_when_unsafe() -> None:
    class _UnsafeIngestionService:
        def ensure_task_delete_safe(self, *, task_id: int, engagement_id: int | None):
            assert task_id == 55
            assert engagement_id == 9
            return {
                "safe": False,
                "catchup_attempted": True,
                "unsafe_execution_ids": ["exec-1"],
                "reason": "Delete blocked: durable evidence ingestion/archive is incomplete for executions: exec-1",
            }

    service = TaskCleanupService(
        db=SimpleNamespace(),
        knowledge_ingestion_service=_UnsafeIngestionService(),
    )

    with pytest.raises(HTTPException) as exc:
        service._enforce_delete_safety_preflight(task_id=55, engagement_id=9)

    assert exc.value.status_code == 409
    assert "delete blocked" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_interrupt_service_reports_pending_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TaskInterruptService(db=SimpleNamespace())
    ticket = SimpleNamespace(
        thread_id="thread-12",
        graph_name="deep",
        interrupt_id="int-12",
        checkpoint_id="cp-12",
        interrupt_type="tool_approval",
        payload_snapshot={"type": "tool_approval"},
    )

    class FakeInterruptState:
        async def get_pending_interrupt(self, task_id, graph_name=None, **_kwargs):
            assert task_id == 12
            assert graph_name == "deep"
            return {
                "interrupt_id": "int-12",
                "interrupt_type": "tool_approval",
                "graph_name": "deep",
                "checkpoint_id": "cp-12",
                "payload": {"type": "tool_approval", "tool_name": "shell.exec"},
                "resumable": True,
            }

    monkeypatch.setattr(
        "backend.services.task.interrupt_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: _owned_task(task_id=task_id, user_id=user_id),
    )
    monkeypatch.setattr(service, "_get_authoritative_pending_ticket", lambda task_id: ticket)

    result = await service.get_task_interrupt(
        task_id=12,
        user_id=2,
        interrupt_service=FakeInterruptState(),
        tenant_id=1,
    )
    assert result["has_interrupt"] is True
    assert result["interrupt_id"] == "int-12"
    assert result["graph_name"] == "deep"
    assert result["payload"]["tool_name"] == "shell.exec"


@pytest.mark.asyncio
async def test_interrupt_service_uses_ticket_authority_when_snapshot_disagrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TaskInterruptService(db=SimpleNamespace())

    class FakeInterruptState:
        async def get_pending_interrupt(self, task_id, graph_name=None, **_kwargs):
            assert task_id == 55
            return {"has_interrupt": True, "interrupt_id": "unexpected"}

    monkeypatch.setattr(
        "backend.services.task.interrupt_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: _owned_task(task_id=task_id, user_id=user_id),
    )
    monkeypatch.setattr(service, "_get_authoritative_pending_ticket", lambda task_id: None)

    result = await service.get_task_interrupt(
        task_id=55,
        user_id=2,
        interrupt_service=FakeInterruptState(),
        tenant_id=1,
    )
    assert result == {"has_interrupt": False, "task_id": 55}


@pytest.mark.asyncio
async def test_interrupt_service_resume_raises_when_interrupt_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume requires interrupt_id; omit or empty raises 400."""
    service = TaskInterruptService(db=SimpleNamespace())

    monkeypatch.setattr(
        "backend.services.task.interrupt_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: _owned_task(task_id=task_id, user_id=user_id),
    )

    with pytest.raises(HTTPException) as exc:
        await service.resume_graph_execution(
            task_id=44,
            user_id=7,
            interrupt_id=None,
            graph_name=None,
            response_payload={"action": "approve"},
            create_task_fn=lambda coro: coro.close(),
            run_resume_generation=lambda **kwargs: asyncio.sleep(0),
            tenant_id=1,
        )

    assert exc.value.status_code == 400
    assert "interrupt_id is required" in exc.value.detail


@pytest.mark.asyncio
async def test_interrupt_service_resume_enqueues_generation_and_returns_resumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume with canonical interrupt_id enqueues generation and returns resumed."""
    service = TaskInterruptService(db=SimpleNamespace())
    captured: dict[str, object] = {}

    class FakeWorkflowService:
        def __init__(self, _db):
            self.calls = 0

        def try_begin_resume(self, **kwargs):
            self.calls += 1
            return SimpleNamespace(id=77)

        def ensure_waiting_workflow(self, **kwargs):
            captured["ensure"] = kwargs

        def mark_waiting_for_human(self, **kwargs):
            captured["mark"] = kwargs

    class FakeTicketService:
        def __init__(self, _db):
            pass

        def claim_for_resume(self, **kwargs):
            captured["claim"] = kwargs
            return SimpleNamespace(
                interrupt_id=kwargs["interrupt_id"],
                graph_name="deep",
                checkpoint_id="cp-1",
                interrupt_type="tool_approval",
                payload_snapshot={
                    "turn_id": "turn-5",
                    "turn_sequence": 5,
                    "conversation_id": "conv-1",
                    "reserved_message_id": 321,
                },
            )

    def fake_run_resume_generation(**kwargs):
        captured["kwargs"] = kwargs

        async def _noop():
            return None

        return _noop()

    def fake_create_task(coro):
        captured["scheduled"] = True
        coro.close()

    monkeypatch.setattr(
        "backend.services.task.interrupt_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: _owned_task(task_id=task_id, user_id=user_id),
    )
    monkeypatch.setattr(
        "backend.services.task.interrupt_service.TurnWorkflowService",
        FakeWorkflowService,
    )
    monkeypatch.setattr(
        "backend.services.task.interrupt_service.InterruptTicketService",
        FakeTicketService,
    )

    result = await service.resume_graph_execution(
        task_id=77,
        user_id=8,
        interrupt_id="deep:checkpoint:cp-1",
        graph_name=None,
        response_payload={"action": "approve"},
        create_task_fn=fake_create_task,
        run_resume_generation=fake_run_resume_generation,
        tenant_id=1,
    )

    assert result["status"] == "resumed"
    assert result["task_id"] == 77
    assert result["interrupt_id"] == "deep:checkpoint:cp-1"
    assert "deprecation" not in result
    assert captured["scheduled"] is True
    assert captured["claim"]["interrupt_id"] == "deep:checkpoint:cp-1"
    assert captured["kwargs"]["workflow_id"] == 77
    assert captured["kwargs"]["checkpoint_id"] == "cp-1"
    assert captured["kwargs"]["interrupt_id"] == "deep:checkpoint:cp-1"
    assert captured["kwargs"]["tenant_id"] == 1
    assert captured["kwargs"]["runtime_placement_mode"] == "local"
    assert captured["kwargs"]["workspace_id"] == "task-77"
    assert captured["kwargs"]["actor_type"] == "user"
    assert captured["kwargs"]["actor_id"] == "8"
    assert captured["kwargs"]["runner_id"] is None
    assert captured["kwargs"]["execution_site_id"] is None
    assert captured["kwargs"]["user_id"] == 8


@pytest.mark.asyncio
async def test_interrupt_service_resume_ignores_client_graph_name_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Client-provided graph_name must not override canonical ticket graph_name."""
    service = TaskInterruptService(db=SimpleNamespace())
    captured: dict[str, object] = {}

    class FakeWorkflowService:
        def __init__(self, _db):
            pass

        def try_begin_resume(self, **kwargs):
            captured["workflow"] = kwargs
            return SimpleNamespace(id=303)

        def ensure_waiting_workflow(self, **kwargs):
            captured["ensure"] = kwargs

        def mark_waiting_for_human(self, **kwargs):
            captured["mark"] = kwargs

    class FakeTicketService:
        def __init__(self, _db):
            pass

        def claim_for_resume(self, **kwargs):
            return SimpleNamespace(
                interrupt_id=kwargs["interrupt_id"],
                graph_name="deep_reasoning",
                checkpoint_id="cp-dr-55",
                interrupt_type="plan_review",
                payload_snapshot={
                    "turn_id": "task-55-turn-8",
                    "turn_sequence": 8,
                },
            )

    class _NoSnapshotService:
        async def get_pending_interrupt(self, task_id, graph_name=None, **_kwargs):
            return None

    def fake_run_resume_generation(**kwargs):
        captured["resume"] = kwargs

        async def _noop():
            return None

        return _noop()

    def fake_create_task(coro):
        captured["scheduled"] = True
        coro.close()

    monkeypatch.setattr(
        "backend.services.task.interrupt_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: _owned_task(task_id=task_id, user_id=user_id),
    )
    monkeypatch.setattr(
        "backend.services.task.interrupt_service.TurnWorkflowService",
        FakeWorkflowService,
    )
    monkeypatch.setattr(
        "backend.services.task.interrupt_service.InterruptTicketService",
        FakeTicketService,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.interrupt_state_service.get_interrupt_state_service",
        lambda: _NoSnapshotService(),
    )

    result = await service.resume_graph_execution(
        task_id=55,
        user_id=3,
        interrupt_id="intr-graph-mismatch-1",
        graph_name="simple_tool",
        response_payload={"action": "approve"},
        create_task_fn=fake_create_task,
        run_resume_generation=fake_run_resume_generation,
        tenant_id=1,
    )

    assert result["status"] == "resumed"
    assert captured["resume"]["graph_name"] == "deep_reasoning"
    assert captured["workflow"]["graph_name"] == "deep_reasoning"


@pytest.mark.asyncio
async def test_interrupt_service_resume_does_not_use_run_id_as_checkpoint_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ticket checkpoint must not be synthesized from payload run_id."""
    db = SimpleNamespace()
    db.commit = lambda: None
    db.rollback = lambda: None
    service = TaskInterruptService(db=db)
    captured: dict[str, object] = {}

    class FakeWorkflowService:
        def __init__(self, _db):
            pass

        def try_begin_resume(self, **kwargs):
            captured["workflow"] = kwargs
            return SimpleNamespace(id=301)

        def ensure_waiting_workflow(self, **kwargs):
            captured["ensure"] = kwargs

        def mark_waiting_for_human(self, **kwargs):
            captured["mark"] = kwargs

    class FakeTicketService:
        def __init__(self, _db):
            pass

        def claim_for_resume(self, **kwargs):
            return SimpleNamespace(
                interrupt_id=kwargs["interrupt_id"],
                graph_name="deep_reasoning",
                checkpoint_id=None,
                interrupt_type="plan_review",
                payload_snapshot={
                    "run_id": 77,  # NOT a checkpoint identity
                    "turn_id": "task-99-turn-3",
                    "turn_sequence": 3,
                },
            )

    class _NoSnapshotService:
        async def get_pending_interrupt(self, task_id, graph_name=None, **_kwargs):
            captured["snapshot_query"] = {"task_id": task_id, "graph_name": graph_name}
            return None

    def fake_run_resume_generation(**kwargs):
        captured["resume"] = kwargs

        async def _noop():
            return None

        return _noop()

    def fake_create_task(coro):
        captured["scheduled"] = True
        coro.close()

    monkeypatch.setattr(
        "backend.services.task.interrupt_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: _owned_task(task_id=task_id, user_id=user_id),
    )
    monkeypatch.setattr(
        "backend.services.task.interrupt_service.TurnWorkflowService",
        FakeWorkflowService,
    )
    monkeypatch.setattr(
        "backend.services.task.interrupt_service.InterruptTicketService",
        FakeTicketService,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.interrupt_state_service.get_interrupt_state_service",
        lambda: _NoSnapshotService(),
    )

    result = await service.resume_graph_execution(
        task_id=99,
        user_id=5,
        interrupt_id="deep_reasoning:turn:task-99-turn-3",
        graph_name=None,
        response_payload={"action": "approve"},
        create_task_fn=fake_create_task,
        run_resume_generation=fake_run_resume_generation,
        tenant_id=1,
    )

    assert result["status"] == "resumed"
    assert captured["scheduled"] is True
    assert captured["resume"]["checkpoint_id"] is None
    assert captured["resume"]["resume_key"] == "deep_reasoning:turn:task-99-turn-3"
    assert captured["workflow"]["checkpoint_id"] is None


@pytest.mark.asyncio
async def test_interrupt_service_resume_hydrates_checkpoint_id_from_pending_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ticket checkpoint should be hydrated from canonical pending interrupt snapshot."""
    db = SimpleNamespace()
    db.commit = lambda: None
    db.rollback = lambda: None
    service = TaskInterruptService(db=db)
    captured: dict[str, object] = {}

    claimed_ticket = SimpleNamespace(
        interrupt_id="simple_tool:checkpoint:cp-live-1",
        graph_name="simple_tool",
        checkpoint_id=None,
        interrupt_type="tool_approval",
        payload_snapshot={
            "turn_id": "task-42-turn-6",
            "turn_sequence": 6,
            "reserved_message_id": 906,
        },
    )

    class FakeWorkflowService:
        def __init__(self, _db):
            pass

        def try_begin_resume(self, **kwargs):
            captured["workflow"] = kwargs
            return SimpleNamespace(id=302)

        def ensure_waiting_workflow(self, **kwargs):
            captured["ensure"] = kwargs

        def mark_waiting_for_human(self, **kwargs):
            captured["mark"] = kwargs

    class FakeTicketService:
        def __init__(self, _db):
            pass

        def claim_for_resume(self, **kwargs):
            return claimed_ticket

    class _SnapshotService:
        async def get_pending_interrupt(self, task_id, graph_name=None, **_kwargs):
            captured["snapshot_query"] = {"task_id": task_id, "graph_name": graph_name}
            return {
                "task_id": task_id,
                "graph_name": "simple_tool",
                "interrupt_id": "simple_tool:checkpoint:cp-live-1",
                "checkpoint_id": "cp-live-1",
            }

    def fake_run_resume_generation(**kwargs):
        captured["resume"] = kwargs

        async def _noop():
            return None

        return _noop()

    def fake_create_task(coro):
        captured["scheduled"] = True
        coro.close()

    monkeypatch.setattr(
        "backend.services.task.interrupt_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: _owned_task(task_id=task_id, user_id=user_id),
    )
    monkeypatch.setattr(
        "backend.services.task.interrupt_service.TurnWorkflowService",
        FakeWorkflowService,
    )
    monkeypatch.setattr(
        "backend.services.task.interrupt_service.InterruptTicketService",
        FakeTicketService,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.interrupt_state_service.get_interrupt_state_service",
        lambda: _SnapshotService(),
    )

    result = await service.resume_graph_execution(
        task_id=42,
        user_id=5,
        interrupt_id="simple_tool:checkpoint:cp-live-1",
        graph_name=None,
        response_payload={"action": "approve"},
        create_task_fn=fake_create_task,
        run_resume_generation=fake_run_resume_generation,
        tenant_id=1,
    )

    assert result["status"] == "resumed"
    assert claimed_ticket.checkpoint_id == "cp-live-1"
    assert captured["resume"]["checkpoint_id"] == "cp-live-1"
    assert captured["resume"]["resume_key"] == "cp-live-1"
    assert captured["workflow"]["checkpoint_id"] == "cp-live-1"


@pytest.mark.asyncio
async def test_interrupt_service_resume_reconciles_stale_ticket_checkpoint_from_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical snapshot checkpoint should override stale ticket checkpoint cache."""
    db = SimpleNamespace()
    db.commit = lambda: None
    db.rollback = lambda: None
    service = TaskInterruptService(db=db)
    captured: dict[str, object] = {}

    claimed_ticket = SimpleNamespace(
        interrupt_id="intr-stale-1",
        graph_name="simple_tool",
        checkpoint_id="cp-stale",
        interrupt_type="tool_approval",
        payload_snapshot={
            "turn_id": "task-11-turn-4",
            "turn_sequence": 4,
        },
    )

    class FakeWorkflowService:
        def __init__(self, _db):
            pass

        def try_begin_resume(self, **kwargs):
            captured["workflow"] = kwargs
            return SimpleNamespace(id=401)

        def ensure_waiting_workflow(self, **kwargs):
            captured["ensure"] = kwargs

        def mark_waiting_for_human(self, **kwargs):
            captured["mark"] = kwargs

    class FakeTicketService:
        def __init__(self, _db):
            pass

        def claim_for_resume(self, **kwargs):
            return claimed_ticket

    class _SnapshotService:
        async def get_pending_interrupt(self, task_id, graph_name=None, **_kwargs):
            captured["snapshot_query"] = {"task_id": task_id, "graph_name": graph_name}
            return {
                "task_id": task_id,
                "graph_name": "simple_tool",
                "interrupt_id": "intr-stale-1",
                "checkpoint_id": "cp-live",
            }

    def fake_run_resume_generation(**kwargs):
        captured["resume"] = kwargs

        async def _noop():
            return None

        return _noop()

    def fake_create_task(coro):
        captured["scheduled"] = True
        coro.close()

    monkeypatch.setattr(
        "backend.services.task.interrupt_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: _owned_task(task_id=task_id, user_id=user_id),
    )
    monkeypatch.setattr(
        "backend.services.task.interrupt_service.TurnWorkflowService",
        FakeWorkflowService,
    )
    monkeypatch.setattr(
        "backend.services.task.interrupt_service.InterruptTicketService",
        FakeTicketService,
    )
    monkeypatch.setattr(
        "backend.services.langgraph_chat.checkpoint.interrupt_state_service.get_interrupt_state_service",
        lambda: _SnapshotService(),
    )

    result = await service.resume_graph_execution(
        task_id=11,
        user_id=2,
        interrupt_id="intr-stale-1",
        graph_name=None,
        response_payload={"action": "approve"},
        create_task_fn=fake_create_task,
        run_resume_generation=fake_run_resume_generation,
        tenant_id=1,
    )

    assert result["status"] == "resumed"
    assert claimed_ticket.checkpoint_id == "cp-live"
    assert captured["resume"]["checkpoint_id"] == "cp-live"
    assert captured["resume"]["resume_key"] == "cp-live"
    assert captured["workflow"]["checkpoint_id"] == "cp-live"


@pytest.mark.asyncio
async def test_interrupt_service_resume_rejects_claim_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TaskInterruptService(db=SimpleNamespace())

    class FakeTicketService:
        def __init__(self, _db):
            pass

        def claim_for_resume(self, **kwargs):
            raise InterruptTicketClaimConflictError("ticket already RESUMING")

    monkeypatch.setattr(
        "backend.services.task.interrupt_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: _owned_task(task_id=task_id, user_id=user_id),
    )
    monkeypatch.setattr(
        "backend.services.task.interrupt_service.InterruptTicketService",
        FakeTicketService,
    )

    with pytest.raises(HTTPException) as exc:
        await service.resume_graph_execution(
            task_id=77,
            user_id=8,
            interrupt_id="deep:checkpoint:cp-1",
            graph_name=None,
            response_payload={"approved": True},
            create_task_fn=lambda coro: coro.close(),
            run_resume_generation=lambda **kwargs: asyncio.sleep(0),
            tenant_id=1,
        )

    assert exc.value.status_code == 409
    assert "no longer pending" in exc.value.detail


@pytest.mark.asyncio
async def test_interrupt_service_resume_enqueue_failure_reverts_ticket_to_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TaskInterruptService(db=SimpleNamespace())
    captured: dict[str, object] = {}

    class FakeWorkflowService:
        def __init__(self, _db):
            pass

        def try_begin_resume(self, **kwargs):
            return SimpleNamespace(id=999)

        def ensure_waiting_workflow(self, **kwargs):
            captured["ensure"] = kwargs

        def mark_waiting_for_human(self, **kwargs):
            captured["mark_waiting"] = kwargs

    class FakeTicketService:
        def __init__(self, _db):
            pass

        def claim_for_resume(self, **kwargs):
            captured["claim"] = kwargs
            return SimpleNamespace(
                interrupt_id=kwargs["interrupt_id"],
                graph_name="deep",
                checkpoint_id="cp-9",
                interrupt_type="tool_approval",
                payload_snapshot={
                    "turn_id": "turn-9",
                    "turn_sequence": 9,
                    "conversation_id": "conv-9",
                    "reserved_message_id": 909,
                },
            )

        def mark_pending(self, **kwargs):
            captured["mark_pending"] = kwargs

    def fake_run_resume_generation(**kwargs):
        captured["resume_kwargs"] = kwargs

        async def _noop():
            return None

        return _noop()

    def fake_create_task(coro):
        coro.close()
        raise RuntimeError("enqueue boom")

    monkeypatch.setattr(
        "backend.services.task.interrupt_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: _owned_task(task_id=task_id, user_id=user_id),
    )
    monkeypatch.setattr(
        "backend.services.task.interrupt_service.TurnWorkflowService",
        FakeWorkflowService,
    )
    monkeypatch.setattr(
        "backend.services.task.interrupt_service.InterruptTicketService",
        FakeTicketService,
    )

    with pytest.raises(RuntimeError, match="enqueue boom"):
        await service.resume_graph_execution(
            task_id=77,
            user_id=8,
            interrupt_id="deep:checkpoint:cp-9",
            graph_name=None,
            response_payload={"action": "approve"},
            create_task_fn=fake_create_task,
            run_resume_generation=fake_run_resume_generation,
            tenant_id=1,
        )

    assert captured["mark_waiting"]["workflow_id"] == 999
    assert captured["mark_waiting"]["resume_key"] == "cp-9"
    assert captured["mark_pending"] == {
        "interrupt_id": "deep:checkpoint:cp-9",
        "task_id": 77,
    }


@pytest.mark.asyncio
async def test_interrupt_service_invalid_clarify_payload_reverts_claim_to_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = TaskInterruptService(db=SimpleNamespace())
    captured: dict[str, object] = {}

    class FakeTicketService:
        def __init__(self, _db):
            pass

        def claim_for_resume(self, **kwargs):
            captured["claim"] = kwargs
            return SimpleNamespace(
                interrupt_id=kwargs["interrupt_id"],
                graph_name="deep",
                checkpoint_id="cp-clarify-9",
                interrupt_type="clarify_request",
                payload_snapshot={"type": "clarify_request"},
            )

        def mark_pending(self, **kwargs):
            captured["mark_pending"] = kwargs

    monkeypatch.setattr(
        "backend.services.task.interrupt_service.get_owned_task_or_404",
        lambda db, task_id, user_id, tenant_id: _owned_task(task_id=task_id, user_id=user_id),
    )
    monkeypatch.setattr(
        "backend.services.task.interrupt_service.InterruptTicketService",
        FakeTicketService,
    )

    with pytest.raises(HTTPException) as exc:
        await service.resume_graph_execution(
            task_id=88,
            user_id=9,
            interrupt_id="clarify:checkpoint:cp-clarify-9",
            graph_name=None,
            response_payload={"action": "approve"},
            create_task_fn=lambda coro: coro.close(),
            run_resume_generation=lambda **kwargs: asyncio.sleep(0),
            tenant_id=1,
        )

    assert exc.value.status_code == 400
    assert "clarify_request interrupts require action='answer'" in exc.value.detail
    assert captured["mark_pending"] == {
        "interrupt_id": "clarify:checkpoint:cp-clarify-9",
        "task_id": 88,
    }
