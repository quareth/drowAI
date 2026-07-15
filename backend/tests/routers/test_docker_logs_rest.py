"""Regression tests for docker compatibility route error semantics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.routers import docker_logs_rest
from backend.models.core import TaskStatus
from backend.services.runtime_provider.snapshot_normalization import (
    normalize_runtime_startup_progress_snapshot,
)
from backend.services.tenant.context import TenantRequestContext


def _tenant_context(*, user_id: int, tenant_id: int = 701, role: str = "owner") -> TenantRequestContext:
    return TenantRequestContext(
        tenant_id=tenant_id,
        user_id=user_id,
        role=role,
        membership_id=1,
        is_default_tenant=False,
    )


def _runtime_task(
    *,
    task_id: int,
    user_id: int = 9,
    tenant_id: int = 701,
    placement: str = "local",
    status: str = TaskStatus.RUNNING.value,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        user_id=user_id,
        tenant_id=tenant_id,
        workspace_id=f"task-{task_id}",
        runtime_placement_mode=placement,
        graph_thread_id="a" * 32,
        runner_id="runner-1" if placement == "runner" else None,
        execution_site_id="site-1" if placement == "runner" else None,
        status=status,
        engagement_id=None,
    )


def test_startup_progress_normalization_uses_runtime_wording() -> None:
    progress = normalize_runtime_startup_progress_snapshot(
        {"job_status": "running", "container_status": "running"}
    )

    assert progress is not None
    assert progress["message"] == "Runtime is now running. Streaming logs..."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "kwargs"),
    [
        (docker_logs_rest.get_docker_compose_logs, {}),
        (docker_logs_rest.get_container_startup_progress, {}),
        (docker_logs_rest.execute_command, {"command": "id"}),
        (docker_logs_rest.stop_container, {}),
        (docker_logs_rest.get_container_metrics, {}),
        (docker_logs_rest.get_container_status, {}),
    ],
)
async def test_docker_routes_preserve_authorization_http_exception(
    monkeypatch: pytest.MonkeyPatch,
    route,
    kwargs,
) -> None:
    """Ownership failures should not be wrapped as generic 500 responses."""

    def _raise_not_found(**_kwargs):
        raise HTTPException(status_code=404, detail="Task not found")

    monkeypatch.setattr(docker_logs_rest, "get_tenant_task_or_404", _raise_not_found)

    with pytest.raises(HTTPException) as exc:
        await route(
            task_id=123,
            current_user=SimpleNamespace(id=7),
            tenant_context=_tenant_context(user_id=7),
            db=object(),
            **kwargs,
        )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Task not found"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "kwargs"),
    [
        (docker_logs_rest.get_docker_compose_logs, {}),
        (docker_logs_rest.get_container_startup_progress, {}),
        (docker_logs_rest.execute_command, {"command": "id"}),
        (docker_logs_rest.cleanup_runtime_workspace, {}),
        (docker_logs_rest.get_container_metrics, {}),
        (docker_logs_rest.get_container_status, {}),
    ],
)
async def test_runtime_provider_routes_reject_product_local_before_local_provider(
    monkeypatch: pytest.MonkeyPatch,
    route,
    kwargs,
) -> None:
    """Product-scoped runtime routes must reject local placement before provider lookup."""

    monkeypatch.setattr(
        docker_logs_rest,
        "get_tenant_task_or_404",
        lambda **_kwargs: _runtime_task(task_id=61),
    )

    with pytest.raises(HTTPException) as exc:
        await route(
            task_id=61,
            current_user=SimpleNamespace(id=9),
            tenant_context=_tenant_context(user_id=9),
            db=object(),
            **kwargs,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["reason_code"] == "PRODUCT_LOCAL_PLACEMENT_FORBIDDEN"
    assert exc.value.detail["placement"] == "local"
    assert exc.value.detail["scope"] == "product_task"


@pytest.mark.asyncio
async def test_stop_container_rejects_product_local_before_retirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop route should preserve the task runtime service local-placement rejection."""

    task = _runtime_task(task_id=62, status=TaskStatus.RUNNING.value)
    monkeypatch.setattr(docker_logs_rest, "get_tenant_task_or_404", lambda **_kwargs: task)
    monkeypatch.setattr(
        "backend.services.task.runtime_service.get_owned_task_or_404",
        lambda **_kwargs: task,
    )

    class _ForbiddenStateService:
        def __init__(self, _db):
            raise AssertionError("state transitions must not run before local placement rejection")

    monkeypatch.setattr(
        "backend.services.task.runtime_service.TaskStateService",
        _ForbiddenStateService,
    )

    with pytest.raises(HTTPException) as exc:
        await docker_logs_rest.stop_container(
            task_id=62,
            current_user=SimpleNamespace(id=9),
            tenant_context=_tenant_context(user_id=9),
            db=object(),
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["reason_code"] == "PRODUCT_LOCAL_RUNTIME_PLACEMENT_REJECTED"


@pytest.mark.asyncio
async def test_docker_compose_status_uses_provider_diagnostic_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global status route should use provider output instead of hardcoded health."""

    class _FakeProvider:
        async def list_runtime_inventory(self, request):
            assert request.runtime_call_scope is docker_logs_rest.RuntimeCallScope.DIAGNOSTIC
            assert request.metadata["runtime_call_scope"] == "diagnostic"
            return SimpleNamespace(
                ok=True,
                provider="fake_provider",
                metadata={"delegate_result": {"total": 3, "containers": ["hidden"]}},
            )

    class _FakeRegistry:
        def get_provider(self, **_kwargs):
            return _FakeProvider()

    monkeypatch.setattr(docker_logs_rest, "RuntimeProviderRegistry", _FakeRegistry)

    response = await docker_logs_rest.get_docker_compose_status(current_user=SimpleNamespace(id=42))

    assert response["status"] == "diagnostic"
    assert response["docker_available"] is True
    assert response["scope"] == "local_diagnostic"
    assert response["provider"] == "fake_provider"
    assert response["inventory_total"] == 3
    assert "containers" not in response


@pytest.mark.asyncio
async def test_docker_compose_status_reports_unavailable_on_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider failure should report unavailable diagnostics without false healthy state."""

    class _FakeProvider:
        async def list_runtime_inventory(self, _request):
            return SimpleNamespace(
                ok=False,
                provider="fake_provider",
                metadata={},
            )

    class _FakeRegistry:
        def get_provider(self, **_kwargs):
            return _FakeProvider()

    monkeypatch.setattr(docker_logs_rest, "RuntimeProviderRegistry", _FakeRegistry)

    response = await docker_logs_rest.get_docker_compose_status(current_user=SimpleNamespace(id=42))

    assert response["status"] == "unavailable"
    assert response["docker_available"] is False
    assert response["scope"] == "local_diagnostic"
    assert response["provider"] == "fake_provider"
    assert response["inventory_total"] is None


@pytest.mark.asyncio
async def test_stop_container_runner_pending_returns_stopping_without_forced_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner-accepted stop should remain transitional until runtime events finalize it."""

    monkeypatch.setattr(
        docker_logs_rest,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=34, status=TaskStatus.RUNNING.value),
    )

    class _FakeRuntimeService:
        def __init__(self, _db):
            pass

        async def stop_task(self, *, task_id: int, user_id: int, tenant_id: int | None = None):
            assert task_id == 34
            assert user_id == 9
            assert tenant_id == 701
            return SimpleNamespace(status=TaskStatus.STOPPING.value)

    monkeypatch.setattr(docker_logs_rest, "TaskRuntimeService", _FakeRuntimeService)

    response = await docker_logs_rest.stop_container(
        task_id=34,
        current_user=SimpleNamespace(id=9),
        tenant_context=_tenant_context(user_id=9),
        db=object(),
    )

    assert response["status"] == "stopping"
    assert "awaiting runner confirmation" in response["logs"][0]["message"]


@pytest.mark.asyncio
async def test_stop_container_completed_stop_returns_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compatibility stop route should report stopped when runtime service finalizes stop."""

    monkeypatch.setattr(
        docker_logs_rest,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=35, status=TaskStatus.RUNNING.value),
    )

    class _FakeRuntimeService:
        def __init__(self, _db):
            pass

        async def stop_task(self, *, task_id: int, user_id: int, tenant_id: int | None = None):
            assert task_id == 35
            assert user_id == 10
            assert tenant_id == 701
            return SimpleNamespace(status=TaskStatus.STOPPED.value)

    monkeypatch.setattr(docker_logs_rest, "TaskRuntimeService", _FakeRuntimeService)

    response = await docker_logs_rest.stop_container(
        task_id=35,
        current_user=SimpleNamespace(id=10),
        tenant_context=_tenant_context(user_id=10),
        db=object(),
    )

    assert response["status"] == "stopped"
    assert response["logs"][0]["message"] == "Runtime stopped"


@pytest.mark.asyncio
async def test_get_docker_compose_logs_fails_closed_on_provider_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner log snapshot failures should propagate deterministic provider errors."""

    monkeypatch.setattr(
        docker_logs_rest,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=51),
    )

    class _FakeRuntimeOperations:
        def __init__(self, _db):
            pass

        async def run_authorized_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=False,
                provider="cloud_runner",
                status=SimpleNamespace(value="rejected"),
                error_code="RUNNER_OPERATION_RESULT_TIMEOUT",
                error_message="timed out",
                metadata={},
            )

    monkeypatch.setattr(docker_logs_rest, "RuntimeOperationService", _FakeRuntimeOperations)

    with pytest.raises(HTTPException) as exc:
        await docker_logs_rest.get_docker_compose_logs(
            task_id=51,
            current_user=SimpleNamespace(id=9),
            tenant_context=_tenant_context(user_id=9),
            db=object(),
        )

    assert exc.value.status_code == 504
    assert "RUNNER_OPERATION_RESULT_TIMEOUT" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_get_docker_compose_logs_normalizes_runner_snapshot_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner log snapshot mappings should be projected to the route's logs list shape."""

    monkeypatch.setattr(
        docker_logs_rest,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=52),
    )

    class _FakeRuntimeOperations:
        def __init__(self, _db):
            pass

        async def run_authorized_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "runtime_job_id": "job-52",
                        "logs": [{"message": "runner-log"}],
                    }
                },
            )

    monkeypatch.setattr(docker_logs_rest, "RuntimeOperationService", _FakeRuntimeOperations)

    response = await docker_logs_rest.get_docker_compose_logs(
        task_id=52,
        current_user=SimpleNamespace(id=9),
        tenant_context=_tenant_context(user_id=9),
        db=object(),
    )

    assert response["logs"] == [{"message": "runner-log"}]
    assert response["total_lines"] == 1


@pytest.mark.asyncio
async def test_execute_command_fails_closed_for_runner_local_only_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local-only execute-command compatibility route must expose deterministic provider errors."""

    monkeypatch.setattr(
        docker_logs_rest,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=58),
    )

    class _FakeRuntimeOperations:
        def __init__(self, _db):
            pass

        async def run_authorized_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=False,
                provider="cloud_runner",
                status=SimpleNamespace(value="rejected"),
                error_code="RUNNER_TOOLING_PLANE_DEFERRED",
                error_message="Runner-placement runtime command execution is local-only.",
                metadata={},
            )

    monkeypatch.setattr(docker_logs_rest, "RuntimeOperationService", _FakeRuntimeOperations)

    with pytest.raises(HTTPException) as exc:
        await docker_logs_rest.execute_command(
            task_id=58,
            command="id",
            current_user=SimpleNamespace(id=9),
            tenant_context=_tenant_context(user_id=9),
            db=object(),
        )

    assert exc.value.status_code == 503
    assert "RUNNER_TOOLING_PLANE_DEFERRED" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_cleanup_runtime_workspace_normalizes_runner_success_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup compatibility route should expose provider-mediated success payloads."""

    monkeypatch.setattr(
        docker_logs_rest,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=59),
    )

    class _FakeRuntimeOperations:
        def __init__(self, _db):
            pass

        async def run_authorized_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "cleaned": True,
                    }
                },
            )

    monkeypatch.setattr(docker_logs_rest, "RuntimeOperationService", _FakeRuntimeOperations)

    response = await docker_logs_rest.cleanup_runtime_workspace(
        task_id=59,
        cleanup_scope="runtime",
        retain_outputs=False,
        current_user=SimpleNamespace(id=9),
        tenant_context=_tenant_context(user_id=9),
        db=object(),
    )

    assert response == {
        "task_id": 59,
        "success": True,
        "cleanup_scope": "runtime",
        "retain_outputs": False,
    }


@pytest.mark.asyncio
async def test_cleanup_runtime_workspace_fails_closed_on_provider_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup compatibility route should preserve deterministic timeout errors."""

    monkeypatch.setattr(
        docker_logs_rest,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=60),
    )

    class _FakeRuntimeOperations:
        def __init__(self, _db):
            pass

        async def run_authorized_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=False,
                provider="cloud_runner",
                status=SimpleNamespace(value="rejected"),
                error_code="RUNNER_OPERATION_RESULT_TIMEOUT",
                error_message="cleanup timeout",
                metadata={},
            )

    monkeypatch.setattr(docker_logs_rest, "RuntimeOperationService", _FakeRuntimeOperations)

    with pytest.raises(HTTPException) as exc:
        await docker_logs_rest.cleanup_runtime_workspace(
            task_id=60,
            current_user=SimpleNamespace(id=9),
            tenant_context=_tenant_context(user_id=9),
            db=object(),
        )

    assert exc.value.status_code == 504
    assert "RUNNER_OPERATION_RESULT_TIMEOUT" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_get_container_startup_progress_fails_closed_on_provider_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup progress snapshot failures should preserve provider timeout semantics."""

    monkeypatch.setattr(
        docker_logs_rest,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=53),
    )

    class _FakeRuntimeOperations:
        def __init__(self, _db):
            pass

        async def run_authorized_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=False,
                provider="cloud_runner",
                status=SimpleNamespace(value="rejected"),
                error_code="RUNNER_OPERATION_RESULT_TIMEOUT",
                error_message="progress timeout",
                metadata={},
            )

    monkeypatch.setattr(docker_logs_rest, "RuntimeOperationService", _FakeRuntimeOperations)

    with pytest.raises(HTTPException) as exc:
        await docker_logs_rest.get_container_startup_progress(
            task_id=53,
            current_user=SimpleNamespace(id=9),
            tenant_context=_tenant_context(user_id=9),
            db=object(),
        )

    assert exc.value.status_code == 504
    assert "RUNNER_OPERATION_RESULT_TIMEOUT" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_get_container_metrics_normalizes_runner_nested_metrics_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner metrics snapshots should return the client-facing metrics contract."""

    monkeypatch.setattr(
        docker_logs_rest,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=54),
    )

    class _FakeRuntimeOperations:
        def __init__(self, _db):
            pass

        async def run_authorized_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "runtime_job_id": "job-54",
                        "metrics": {
                            "memory_usage": 128 * 1024 * 1024,
                            "memory_limit": 1024 * 1024 * 1024,
                            "cpu_percent": 2.5,
                            "status": "running",
                            "container_running": True,
                        },
                    }
                },
            )

    monkeypatch.setattr(docker_logs_rest, "RuntimeOperationService", _FakeRuntimeOperations)

    response = await docker_logs_rest.get_container_metrics(
        task_id=54,
        current_user=SimpleNamespace(id=9),
        tenant_context=_tenant_context(user_id=9),
        db=object(),
    )

    metrics = response["metrics"]
    assert metrics["cpu_percent"] == 2.5
    assert metrics["memory_usage_mb"] == 128.0
    assert metrics["memory_limit_mb"] == 1024.0
    assert metrics["memory_percent"] == 12.5
    assert metrics["storage"]["used_mb"] == 0.0
    assert metrics["network"] == {"rx_bytes": 0, "tx_bytes": 0}
    assert metrics["status"] == "running"
    assert metrics["container_running"] is True


@pytest.mark.asyncio
async def test_get_container_metrics_fails_closed_on_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compatibility metrics route should fail closed for runner snapshot failures."""

    monkeypatch.setattr(
        docker_logs_rest,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=55),
    )

    class _FakeRuntimeOperations:
        def __init__(self, _db):
            pass

        async def run_authorized_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=False,
                provider="cloud_runner",
                status=SimpleNamespace(value="failed"),
                error_code="RUNNER_RUNTIME_OPERATION_FAILED",
                error_message="metrics unavailable",
                metadata={},
            )

    monkeypatch.setattr(docker_logs_rest, "RuntimeOperationService", _FakeRuntimeOperations)

    with pytest.raises(HTTPException) as exc:
        await docker_logs_rest.get_container_metrics(
            task_id=55,
            current_user=SimpleNamespace(id=9),
            tenant_context=_tenant_context(user_id=9),
            db=object(),
        )

    assert exc.value.status_code == 503
    assert "RUNNER_RUNTIME_OPERATION_FAILED" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_get_container_status_normalizes_runner_status_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner status snapshots should project container status instead of wrapper dicts."""

    monkeypatch.setattr(
        docker_logs_rest,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=56),
    )

    class _FakeRuntimeOperations:
        def __init__(self, _db):
            pass

        async def run_authorized_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "runtime_job_id": "job-56",
                        "job_status": "running",
                        "container_status": "running",
                    }
                },
            )

    monkeypatch.setattr(docker_logs_rest, "RuntimeOperationService", _FakeRuntimeOperations)

    response = await docker_logs_rest.get_container_status(
        task_id=56,
        current_user=SimpleNamespace(id=9),
        tenant_context=_tenant_context(user_id=9),
        db=object(),
    )

    assert response["status"] == "running"
    assert response["docker_available"] is True


@pytest.mark.asyncio
async def test_get_container_status_fails_closed_on_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compatibility status route should fail closed for runner snapshot failures."""

    monkeypatch.setattr(
        docker_logs_rest,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=57),
    )

    class _FakeRuntimeOperations:
        def __init__(self, _db):
            pass

        async def run_authorized_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=False,
                provider="cloud_runner",
                status=SimpleNamespace(value="rejected"),
                error_code="RUNNER_OPERATION_RESULT_TIMEOUT",
                error_message="status timeout",
                metadata={},
            )

    monkeypatch.setattr(docker_logs_rest, "RuntimeOperationService", _FakeRuntimeOperations)

    with pytest.raises(HTTPException) as exc:
        await docker_logs_rest.get_container_status(
            task_id=57,
            current_user=SimpleNamespace(id=9),
            tenant_context=_tenant_context(user_id=9),
            db=object(),
        )

    assert exc.value.status_code == 504
    assert "RUNNER_OPERATION_RESULT_TIMEOUT" in str(exc.value.detail)
