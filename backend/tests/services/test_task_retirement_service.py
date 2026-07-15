"""Unit tests for task runtime retirement service.

Scope:
- Validate task retirement delegates through runtime-provider operations.
- Ensure delete-time retirement asks for a bounded, validated runner result.
- Ensure provider failures are returned as explicit result messages.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from backend.services.runtime_provider.contracts import (
    RuntimeActorType,
    RuntimeCallScope,
    RuntimeOperationStatus,
    RuntimePlacementMode,
)
from backend.services.task.retirement_service import TaskRetirementService


class _FakeDb:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeRuntimeOperations:
    instances: list["_FakeRuntimeOperations"] = []
    context_runtime_placement_mode = RuntimePlacementMode.RUNNER.value
    result = SimpleNamespace(
        ok=True,
        provider="cloud_runner",
        status=RuntimeOperationStatus.SUCCEEDED,
        error_code=None,
        error_message=None,
    )

    def __init__(self, db: Any) -> None:
        self.db = db
        self.context_calls: list[dict[str, Any]] = []
        self.run_calls: list[dict[str, Any]] = []
        self.__class__.instances.append(self)

    def context_for_internal_task(self, **kwargs: Any):
        self.context_calls.append(dict(kwargs))
        return SimpleNamespace(
            task_id=kwargs["task_id"],
            runtime_placement_mode=self.__class__.context_runtime_placement_mode,
        )

    async def run_for_context(self, **kwargs: Any):
        self.run_calls.append(dict(kwargs))
        return self.__class__.result


@pytest.fixture(autouse=True)
def _reset_fake_runtime_operations() -> None:
    _FakeRuntimeOperations.instances = []
    _FakeRuntimeOperations.context_runtime_placement_mode = RuntimePlacementMode.RUNNER.value
    _FakeRuntimeOperations.result = SimpleNamespace(
        ok=True,
        provider="cloud_runner",
        status=RuntimeOperationStatus.SUCCEEDED,
        error_code=None,
        error_message=None,
    )


@pytest.mark.asyncio
async def test_retire_runtime_delegates_to_provider_with_wait_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = _FakeDb()
    cleaned_task_ids: list[int] = []
    _FakeRuntimeOperations.context_runtime_placement_mode = RuntimePlacementMode.LOCAL.value
    monkeypatch.setattr("backend.database.SessionLocal", lambda: fake_db)

    async def fake_cleanup(*, task_id: int) -> None:
        cleaned_task_ids.append(task_id)

    monkeypatch.setattr(
        TaskRetirementService,
        "cleanup_runtime_stream_state",
        staticmethod(fake_cleanup),
    )
    service = TaskRetirementService(runtime_operations_factory=_FakeRuntimeOperations)

    result = await service.retire_runtime(
        task_id=41,
        user_id=9,
        engagement_id=7,
        runtime_call_scope=RuntimeCallScope.DIAGNOSTIC,
    )

    assert result.success is True
    assert result.message == "Runtime retired for task 41"
    assert fake_db.closed is True
    assert cleaned_task_ids == [41]
    runtime_operations = _FakeRuntimeOperations.instances[0]
    assert runtime_operations.context_calls == [
        {
            "task_id": 41,
            "actor_type": RuntimeActorType.SYSTEM,
            "actor_id": "task_retirement",
            "user_id": 9,
            "runtime_call_scope": RuntimeCallScope.DIAGNOSTIC,
        }
    ]
    run_call = runtime_operations.run_calls[0]
    assert run_call["operation"] == "retire_task_runtime"
    assert run_call["payload"] == {
        "force": True,
        "engagement_id": 7,
        "wait_for_result": True,
    }
    assert run_call["metadata"] == {
        "wait_for_result": True,
        "wait_timeout_seconds": 45.0,
    }
    assert run_call["runtime_call_scope"] is RuntimeCallScope.DIAGNOSTIC


@pytest.mark.asyncio
async def test_retire_runtime_rejects_product_local_context_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db = _FakeDb()
    _FakeRuntimeOperations.context_runtime_placement_mode = RuntimePlacementMode.LOCAL.value
    monkeypatch.setattr("backend.database.SessionLocal", lambda: fake_db)
    service = TaskRetirementService(runtime_operations_factory=_FakeRuntimeOperations)

    result = await service.retire_runtime(task_id=42, user_id=9, engagement_id=7)

    assert result.success is False
    assert "PRODUCT_LOCAL_RUNTIME_PLACEMENT_REJECTED" in result.message
    assert "task_id=42" in result.message
    assert fake_db.closed is True
    runtime_operations = _FakeRuntimeOperations.instances[0]
    assert runtime_operations.run_calls == []


@pytest.mark.asyncio
async def test_retire_runtime_returns_provider_failure_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.database.SessionLocal", lambda: _FakeDb())
    _FakeRuntimeOperations.result = SimpleNamespace(
        ok=False,
        provider="cloud_runner",
        status=RuntimeOperationStatus.REJECTED,
        error_code="RUNNER_ASSIGNMENT_REQUIRED",
        error_message="Runner assignment is missing.",
    )
    service = TaskRetirementService(runtime_operations_factory=_FakeRuntimeOperations)

    result = await service.retire_runtime(task_id=55, engagement_id=None)

    assert result.success is False
    assert result.message == (
        "Failed to retire runtime for task 55 | provider=cloud_runner | "
        "status=rejected | code=RUNNER_ASSIGNMENT_REQUIRED | "
        "error=Runner assignment is missing."
    )


@pytest.mark.asyncio
async def test_retire_runtime_returns_unexpected_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("backend.database.SessionLocal", lambda: _FakeDb())

    class _RaisingRuntimeOperations(_FakeRuntimeOperations):
        async def run_for_context(self, **kwargs: Any):
            del kwargs
            raise RuntimeError("provider unavailable")

    service = TaskRetirementService(runtime_operations_factory=_RaisingRuntimeOperations)

    result = await service.retire_runtime(task_id=88, engagement_id=None)

    assert result.success is False
    assert result.message == (
        "Unexpected runtime retirement error for task 88: provider unavailable"
    )
