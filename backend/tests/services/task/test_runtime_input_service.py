"""Tests for the task runtime-input append and signal service."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.services.task.runtime_input_service import TaskRuntimeInputService


class _FakeRuntimeOperationService:
    delegate_result: dict
    calls: list[dict]

    def __init__(self, _db):
        self.calls = type(self).calls

    def context_for_internal_task(self, **kwargs):
        self.calls.append({"context": kwargs})
        return object()

    async def run_for_context(self, **kwargs):
        self.calls.append(
            {
                "operation": kwargs["operation"],
                "payload": kwargs.get("payload"),
                "metadata": kwargs.get("metadata"),
            }
        )
        return SimpleNamespace(
            ok=bool(type(self).delegate_result.get("success", True)),
            metadata={"delegate_result": type(self).delegate_result},
            error_message=type(self).delegate_result.get("detail"),
        )


def test_strict_persistence_stops_before_signal_on_append_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services.task import runtime_input_service

    service = TaskRuntimeInputService()
    _FakeRuntimeOperationService.delegate_result = {
        "success": False,
        "persisted": False,
        "signal_attempted": False,
        "signal_sent": False,
        "detail": "disk full",
    }
    _FakeRuntimeOperationService.calls = []
    monkeypatch.setattr(runtime_input_service, "RuntimeOperationService", _FakeRuntimeOperationService)

    result = asyncio.run(
        service.append_and_signal(task_id=3, message="__switch_model:gpt-5.2", strict_persistence=True)
    )

    assert result.persisted is False
    assert result.signal_attempted is False
    assert result.signal_sent is False
    assert result.detail == "disk full"
    assert _FakeRuntimeOperationService.calls[-1]["payload"]["strict_persistence"] is True
    assert _FakeRuntimeOperationService.calls[-1]["metadata"]["wait_for_result"] is True


def test_best_effort_persistence_still_signals_when_append_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services.task import runtime_input_service

    service = TaskRuntimeInputService()
    _FakeRuntimeOperationService.delegate_result = {
        "success": True,
        "persisted": False,
        "signal_attempted": True,
        "signal_sent": True,
        "detail": "disk full",
    }
    _FakeRuntimeOperationService.calls = []
    monkeypatch.setattr(runtime_input_service, "RuntimeOperationService", _FakeRuntimeOperationService)

    result = asyncio.run(service.append_and_signal(task_id=4, message="continue", strict_persistence=False))

    assert result.persisted is False
    assert result.signal_attempted is True
    assert result.signal_sent is True
    assert result.detail == "disk full"
    assert _FakeRuntimeOperationService.calls[-1]["payload"]["strict_persistence"] is False


def test_successful_append_writes_runtime_input_and_surfaces_signal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services.task import runtime_input_service

    service = TaskRuntimeInputService()
    _FakeRuntimeOperationService.delegate_result = {
        "success": True,
        "persisted": True,
        "signal_attempted": True,
        "signal_sent": False,
        "detail": "container not running",
    }
    _FakeRuntimeOperationService.calls = []
    monkeypatch.setattr(runtime_input_service, "RuntimeOperationService", _FakeRuntimeOperationService)

    result = asyncio.run(
        service.append_and_signal(
            task_id=9,
            message="__reset_conversation",
            strict_persistence=False,
            metadata={"command": "reset_conversation", "provider": "openai"},
        )
    )

    assert result.persisted is True
    assert result.signal_attempted is True
    assert result.signal_sent is False
    assert result.detail == "container not running"

    payload = _FakeRuntimeOperationService.calls[-1]["payload"]
    assert payload["message"] == "__reset_conversation"
    assert payload["metadata"] == {"command": "reset_conversation", "provider": "openai"}


def test_user_originated_runtime_input_preserves_actor_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services.task import runtime_input_service

    service = TaskRuntimeInputService()
    _FakeRuntimeOperationService.delegate_result = {
        "success": True,
        "persisted": True,
        "signal_attempted": True,
        "signal_sent": True,
    }
    _FakeRuntimeOperationService.calls = []
    monkeypatch.setattr(runtime_input_service, "RuntimeOperationService", _FakeRuntimeOperationService)

    asyncio.run(
        service.append_and_signal(
            task_id=11,
            message="continue",
            strict_persistence=False,
            user_id=42,
        )
    )

    context_call = _FakeRuntimeOperationService.calls[0]["context"]
    assert context_call["actor_type"].value == "user"
    assert context_call["actor_id"] == 42
    assert context_call["user_id"] == 42


def test_runner_missing_job_result_is_surfaceable_to_runtime_input_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services.task import runtime_input_service

    service = TaskRuntimeInputService()
    _FakeRuntimeOperationService.delegate_result = {
        "success": False,
        "persisted": False,
        "signal_attempted": False,
        "signal_sent": False,
        "detail": "Unknown runtime job: job-missing",
    }
    _FakeRuntimeOperationService.calls = []
    monkeypatch.setattr(runtime_input_service, "RuntimeOperationService", _FakeRuntimeOperationService)

    result = asyncio.run(
        service.append_and_signal(task_id=77, message="continue", strict_persistence=False)
    )

    assert result.persisted is False
    assert result.signal_attempted is False
    assert result.signal_sent is False
    assert result.detail == "Unknown runtime job: job-missing"
