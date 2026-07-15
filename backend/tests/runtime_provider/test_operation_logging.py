"""Tests for runtime-provider dispatch logging."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.services.runtime_provider.contracts import (
    RuntimeActorType,
    RuntimeCallScope,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    RuntimePlacementMode,
)
from backend.services.runtime_provider.operations import RuntimeOperationService


class _Provider:
    provider_name = "test-provider"


@pytest.mark.asyncio
async def test_run_for_context_logs_start_and_end(caplog) -> None:
    service = RuntimeOperationService(db=SimpleNamespace(), registry=SimpleNamespace())
    service.provider_for_context = lambda context, runtime_call_scope=None: _Provider()  # type: ignore[method-assign]
    context = SimpleNamespace(
        tenant_id=7,
        task_id=42,
        actor_type=RuntimeActorType.USER,
        actor_id=9,
        user_id=9,
        runtime_placement_mode=RuntimePlacementMode.RUNNER.value,
        workspace_id="task-42",
        runner_id="runner-1",
        execution_site_id="site-1",
    )

    async def _call(_provider, request):
        return RuntimeOperationResult(
            tenant_id=request.tenant_id,
            task_id=request.task_id,
            actor_type=request.actor_type,
            actor_id=request.actor_id,
            runtime_placement_mode=request.runtime_placement_mode,
            workspace_id=request.workspace_id,
            accepted=True,
            provider="test-provider",
            operation=request.operation,
            status=RuntimeOperationStatus.SUCCEEDED,
            runner_id=request.runner_id,
            metadata={"runtime_job_id": "job-1"},
        )

    with caplog.at_level("INFO", logger="backend.services.runtime_provider.operations"):
        result = await service.run_for_context(
            context=context,
            operation="get_runtime_logs",
            call=_call,
        )

    assert result.ok is True
    messages = [record.getMessage() for record in caplog.records]
    assert any("runtime_provider.operation.start" in message for message in messages)
    assert any("tenant_id=7 task_id=42" in message for message in messages)
    assert any("runtime_provider.operation.end" in message for message in messages)
    assert any("runtime_job_id=job-1" in message for message in messages)


def test_build_request_defaults_product_task_scope() -> None:
    service = RuntimeOperationService(db=SimpleNamespace(), registry=SimpleNamespace())
    context = SimpleNamespace(
        tenant_id=7,
        task_id=42,
        actor_type=RuntimeActorType.USER,
        actor_id=9,
        user_id=9,
        runtime_placement_mode=RuntimePlacementMode.RUNNER.value,
        workspace_id="task-42",
        runner_id="runner-1",
        execution_site_id="site-1",
    )

    request = service.build_request(
        context=context,
        operation="get_runtime_logs",
    )

    assert request.runtime_call_scope is RuntimeCallScope.PRODUCT_TASK
    assert request.metadata["runtime_call_scope"] == "product_task"


def test_build_request_rejects_unknown_scope_before_provider_dispatch() -> None:
    service = RuntimeOperationService(db=SimpleNamespace(), registry=SimpleNamespace())
    context = SimpleNamespace(
        tenant_id=7,
        task_id=42,
        actor_type=RuntimeActorType.USER,
        actor_id=9,
        user_id=9,
        runtime_placement_mode=RuntimePlacementMode.RUNNER.value,
        workspace_id="task-42",
        runner_id="runner-1",
        execution_site_id="site-1",
    )

    with pytest.raises(HTTPException) as exc:
        service.build_request(
            context=context,
            operation="get_runtime_logs",
            runtime_call_scope="unknown",
        )

    assert exc.value.status_code == 403
    assert "Unsupported runtime call scope" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_run_for_context_rejects_unknown_scope_before_provider_lookup() -> None:
    class _Registry:
        touched = False

        def get_provider(self, **_kwargs):
            self.touched = True
            raise AssertionError("provider lookup must not run for unknown scopes")

    registry = _Registry()
    service = RuntimeOperationService(db=SimpleNamespace(), registry=registry)  # type: ignore[arg-type]
    context = SimpleNamespace(
        tenant_id=7,
        task_id=42,
        actor_type=RuntimeActorType.USER,
        actor_id=9,
        user_id=9,
        runtime_placement_mode=RuntimePlacementMode.RUNNER.value,
        workspace_id="task-42",
        runner_id="runner-1",
        execution_site_id="site-1",
    )
    provider_call_touched = False

    async def _call(_provider, _request):
        nonlocal provider_call_touched
        provider_call_touched = True
        raise AssertionError("provider call must not run for unknown scopes")

    with pytest.raises(HTTPException) as exc:
        await service.run_for_context(
            context=context,
            operation="get_runtime_logs",
            call=_call,
            runtime_call_scope="unknown",
        )

    assert exc.value.status_code == 403
    assert "Unsupported runtime call scope" in str(exc.value.detail)
    assert registry.touched is False
    assert provider_call_touched is False


@pytest.mark.asyncio
async def test_run_for_context_rejects_product_local_before_provider_lookup() -> None:
    class _Registry:
        touched = False

        def get_provider(self, **_kwargs):
            self.touched = True
            raise AssertionError("provider lookup must not run for product local placement")

    registry = _Registry()
    service = RuntimeOperationService(db=SimpleNamespace(), registry=registry)  # type: ignore[arg-type]
    context = SimpleNamespace(
        tenant_id=7,
        task_id=42,
        actor_type=RuntimeActorType.USER,
        actor_id=9,
        user_id=9,
        runtime_placement_mode=RuntimePlacementMode.LOCAL.value,
        workspace_id="task-42",
        runner_id=None,
        execution_site_id=None,
    )
    provider_call_touched = False

    async def _call(_provider, _request):
        nonlocal provider_call_touched
        provider_call_touched = True
        raise AssertionError("provider call must not run for product local placement")

    with pytest.raises(HTTPException) as exc:
        await service.run_for_context(
            context=context,
            operation="get_runtime_logs",
            call=_call,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["reason_code"] == "PRODUCT_LOCAL_PLACEMENT_FORBIDDEN"
    assert exc.value.detail["task_id"] == 42
    assert registry.touched is False
    assert provider_call_touched is False


@pytest.mark.asyncio
async def test_run_for_context_rejects_unsupported_placement_with_distinct_code() -> None:
    class _Registry:
        touched = False

        def get_provider(self, **_kwargs):
            self.touched = True
            raise AssertionError("provider lookup must not run for invalid placement")

    registry = _Registry()
    service = RuntimeOperationService(db=SimpleNamespace(), registry=registry)  # type: ignore[arg-type]
    context = SimpleNamespace(
        tenant_id=7,
        task_id=42,
        actor_type=RuntimeActorType.USER,
        actor_id=9,
        user_id=9,
        runtime_placement_mode="unexpected",
        workspace_id="task-42",
        runner_id=None,
        execution_site_id=None,
    )
    provider_call_touched = False

    async def _call(_provider, _request):
        nonlocal provider_call_touched
        provider_call_touched = True
        raise AssertionError("provider call must not run for invalid placement")

    with pytest.raises(HTTPException) as exc:
        await service.run_for_context(
            context=context,
            operation="get_runtime_logs",
            call=_call,
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["reason_code"] == "INVALID_RUNTIME_PLACEMENT"
    assert exc.value.detail["reason_code"] != "PRODUCT_LOCAL_PLACEMENT_FORBIDDEN"
    assert registry.touched is False
    assert provider_call_touched is False
