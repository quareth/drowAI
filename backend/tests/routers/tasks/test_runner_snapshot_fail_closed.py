"""Fail-closed regression tests for runner snapshot task routes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.routers.tasks import container as container_routes
from backend.routers.tasks import metrics as metrics_routes
from backend.services.tenant.context import TenantRequestContext


def _tenant_context(*, user_id: int, tenant_id: int = 701, role: str = "owner") -> TenantRequestContext:
    return TenantRequestContext(
        tenant_id=tenant_id,
        user_id=user_id,
        role=role,
        membership_id=1,
        is_default_tenant=False,
    )


@pytest.mark.asyncio
async def test_task_container_status_fails_closed_on_provider_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner status snapshot should not fall back to synthetic not_found results."""

    monkeypatch.setattr(
        container_routes,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=42),
    )

    class _RuntimeOperationsStub:
        @staticmethod
        def context_from_authorized_task(**_kwargs):
            return SimpleNamespace(task_id=42)

        def __init__(self, _db):
            pass

        async def run_for_context(self, **_kwargs):
            return SimpleNamespace(
                ok=False,
                provider="cloud_runner",
                status=SimpleNamespace(value="rejected"),
                error_code="RUNNER_OPERATION_RESULT_TIMEOUT",
                error_message="status timeout",
                metadata={},
            )

    monkeypatch.setattr(container_routes, "RuntimeOperationService", _RuntimeOperationsStub)

    with pytest.raises(HTTPException) as exc:
        await container_routes.get_container_status(
            task_id=42,
            current_user=SimpleNamespace(id=8),
            tenant_context=_tenant_context(user_id=8),
            db=object(),
        )

    assert exc.value.status_code == 504
    assert "RUNNER_OPERATION_RESULT_TIMEOUT" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_task_metrics_route_preserves_provider_error_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metrics snapshot errors should preserve provider error code/detail."""

    monkeypatch.setattr(
        metrics_routes,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=43),
    )

    class _RuntimeOperationsStub:
        def __init__(self, _db):
            pass

        async def run_authorized_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=False,
                provider="cloud_runner",
                status=SimpleNamespace(value="failed"),
                error_code="RUNNER_RUNTIME_OPERATION_FAILED",
                error_message="metrics probe failed",
                metadata={},
            )

    monkeypatch.setattr(metrics_routes, "RuntimeOperationService", _RuntimeOperationsStub)

    with pytest.raises(HTTPException) as exc:
        await metrics_routes.get_task_metrics(
            task_id=43,
            current_user=SimpleNamespace(id=9),
            tenant_context=_tenant_context(user_id=9),
            db=object(),
        )

    assert exc.value.status_code == 503
    assert "RUNNER_RUNTIME_OPERATION_FAILED" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_task_container_status_normalizes_runner_status_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task status route should project runner wrapper snapshots to legacy shape."""

    monkeypatch.setattr(
        container_routes,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=44),
    )

    class _RuntimeOperationsStub:
        @staticmethod
        def context_from_authorized_task(**_kwargs):
            return SimpleNamespace(task_id=44)

        def __init__(self, _db):
            pass

        async def run_for_context(self, **_kwargs):
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "runtime_job_id": "job-44",
                        "job_status": "running",
                        "container_status": "running",
                    }
                },
            )

    monkeypatch.setattr(container_routes, "RuntimeOperationService", _RuntimeOperationsStub)

    response = await container_routes.get_container_status(
        task_id=44,
        current_user=SimpleNamespace(id=8),
        tenant_context=_tenant_context(user_id=8),
        db=object(),
    )

    assert response["container_exists"] is True
    assert response["status"] == "running"
    assert response["details"]["runtime_job_id"] == "job-44"


@pytest.mark.asyncio
async def test_task_container_status_strips_runner_absolute_workspace_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task status compatibility payload must not leak runner host absolute paths."""

    monkeypatch.setattr(
        container_routes,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=46),
    )

    class _RuntimeOperationsStub:
        @staticmethod
        def context_from_authorized_task(**_kwargs):
            return SimpleNamespace(task_id=46)

        def __init__(self, _db):
            pass

        async def run_for_context(self, **_kwargs):
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "runtime_job_id": "job-46",
                        "container_status": "running",
                        "workspace_id": "task-46",
                        "workspace_path": "/var/lib/drowai/tasks/task-46",
                    }
                },
            )

    monkeypatch.setattr(container_routes, "RuntimeOperationService", _RuntimeOperationsStub)

    response = await container_routes.get_container_status(
        task_id=46,
        current_user=SimpleNamespace(id=8),
        tenant_context=_tenant_context(user_id=8),
        db=object(),
    )

    assert response["container_exists"] is True
    assert response["status"] == "running"
    assert response["details"]["workspace_id"] == "task-46"
    assert "workspace_path" not in response["details"]


@pytest.mark.asyncio
async def test_task_metrics_route_normalizes_runner_nested_metrics_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Task metrics route should return the client-facing runner metrics contract."""

    monkeypatch.setattr(
        metrics_routes,
        "get_tenant_task_or_404",
        lambda **_kwargs: SimpleNamespace(id=45),
    )

    class _RuntimeOperationsStub:
        def __init__(self, _db):
            pass

        async def run_authorized_task_operation(self, **_kwargs):
            return SimpleNamespace(
                ok=True,
                metadata={
                    "delegate_result": {
                        "runtime_job_id": "job-45",
                        "metrics": {
                            "memory_usage": 256 * 1024 * 1024,
                            "memory_limit": 2 * 1024 * 1024 * 1024,
                            "cpu_percent": 4.0,
                            "status": "running",
                            "container_running": True,
                        },
                    }
                },
            )

    monkeypatch.setattr(metrics_routes, "RuntimeOperationService", _RuntimeOperationsStub)

    response = await metrics_routes.get_task_metrics(
        task_id=45,
        current_user=SimpleNamespace(id=9),
        tenant_context=_tenant_context(user_id=9),
        db=object(),
    )

    metrics = response["metrics"]
    assert metrics["cpu_percent"] == 4.0
    assert metrics["memory_usage_mb"] == 256.0
    assert metrics["memory_limit_mb"] == 2048.0
    assert metrics["memory_percent"] == 12.5
    assert metrics["storage"]["used_mb"] == 0.0
    assert metrics["network"] == {"rx_bytes": 0, "tx_bytes": 0}
    assert metrics["status"] == "running"
    assert metrics["container_running"] is True
