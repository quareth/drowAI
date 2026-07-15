"""Route-level regressions for pause/resume status broadcasts.

Responsibilities:
- Ensure runner-pending pause/resume responses do not emit final status updates.
- Ensure local completed pause/resume responses still emit final status updates.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.domain.task_lifecycle import TaskStatus
from backend.routers.tasks import runtime as runtime_routes


class _FakeTaskResponse:
    @staticmethod
    def model_validate(task):
        return {"id": task.id, "status": task.status}


class _FakeTaskRuntimeService:
    pause_response = None
    resume_response = None

    def __init__(self, _db):
        pass

    async def pause_task(self, **_kwargs):
        return self.pause_response

    async def resume_task(self, **_kwargs):
        return self.resume_response


def _task(*, task_id: int = 42, status: str) -> SimpleNamespace:
    return SimpleNamespace(id=task_id, status=status)


def _patch_route_dependencies(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    calls: list[tuple[int, str]] = []

    async def _record_broadcast(*, task_id: int, status_value: str) -> None:
        calls.append((task_id, status_value))

    monkeypatch.setattr(runtime_routes, "TaskRuntimeService", _FakeTaskRuntimeService)
    monkeypatch.setattr(runtime_routes, "TaskResponse", _FakeTaskResponse)
    monkeypatch.setattr(runtime_routes, "_broadcast_status_update", _record_broadcast)
    monkeypatch.setattr(runtime_routes, "enforce_tenant_action", lambda **_kwargs: None)
    monkeypatch.setattr(
        runtime_routes,
        "get_tenant_task_or_404",
        lambda **_kwargs: _task(status=TaskStatus.RUNNING.value),
    )
    monkeypatch.setattr(
        runtime_routes,
        "get_task_with_engagement_in_tenant_or_404",
        lambda **_kwargs: _task(status=TaskStatus.RUNNING.value),
    )
    return calls


@pytest.mark.asyncio
async def test_pause_route_skips_final_status_broadcast_for_runner_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_route_dependencies(monkeypatch)
    _FakeTaskRuntimeService.pause_response = _task(status=TaskStatus.PAUSING.value)

    response = await runtime_routes.pause_task(
        task_id=42,
        current_user=SimpleNamespace(id=5),
        tenant_context=SimpleNamespace(tenant_id=1, role="owner"),
        db=object(),
    )

    assert response["id"] == 42
    assert calls == []


@pytest.mark.asyncio
async def test_pause_route_broadcasts_final_status_for_local_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_route_dependencies(monkeypatch)
    _FakeTaskRuntimeService.pause_response = _task(status=TaskStatus.PAUSED.value)

    response = await runtime_routes.pause_task(
        task_id=42,
        current_user=SimpleNamespace(id=5),
        tenant_context=SimpleNamespace(tenant_id=1, role="owner"),
        db=object(),
    )

    assert response["id"] == 42
    assert calls == [(42, TaskStatus.PAUSED.value)]


@pytest.mark.asyncio
async def test_resume_route_skips_final_status_broadcast_for_runner_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_route_dependencies(monkeypatch)
    _FakeTaskRuntimeService.resume_response = _task(status=TaskStatus.RESUMING.value)

    response = await runtime_routes.resume_task(
        task_id=42,
        current_user=SimpleNamespace(id=5),
        tenant_context=SimpleNamespace(tenant_id=1, role="owner"),
        db=object(),
    )

    assert response["id"] == 42
    assert calls == []


@pytest.mark.asyncio
async def test_resume_route_broadcasts_final_status_for_local_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_route_dependencies(monkeypatch)
    _FakeTaskRuntimeService.resume_response = _task(status=TaskStatus.RUNNING.value)

    response = await runtime_routes.resume_task(
        task_id=42,
        current_user=SimpleNamespace(id=5),
        tenant_context=SimpleNamespace(tenant_id=1, role="owner"),
        db=object(),
    )

    assert response["id"] == 42
    assert calls == [(42, TaskStatus.RUNNING.value)]
