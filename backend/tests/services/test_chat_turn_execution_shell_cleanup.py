"""Tests for main-owner shell-session cleanup in turn execution orchestration."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from backend.services.langgraph_chat.contracts import LangGraphChatResult
from backend.services.langgraph_chat.execution.orchestration import (
    orchestrator as orchestration_module,
)
from backend.services.langgraph_chat.execution.orchestration.orchestrator import (
    TurnExecutionOrchestrator,
    _close_main_owner_shell_sessions_for_status,
)


class _ShellCleanupService:
    def __init__(self) -> None:
        self.close_calls: list[dict[str, Any]] = []

    async def close_owner_sessions(self, **kwargs: Any) -> None:
        self.close_calls.append(dict(kwargs))


class _FailingShellCleanupService:
    async def close_owner_sessions(self, **_kwargs: Any) -> None:
        raise RuntimeError("cleanup failed")


class _Hub:
    def set_streaming_state(self, task_id: int, state: bool) -> None:
        return None


class _Lifecycle:
    def __init__(self) -> None:
        self.end_calls: list[dict[str, Any]] = []

    def start_run(self, **_kwargs: Any) -> None:
        return None

    def end_run(self, **kwargs: Any) -> None:
        self.end_calls.append(dict(kwargs))

    def is_cancel_requested(self, **_kwargs: Any) -> bool:
        return False


class _TurnStreamPublisher:
    def set_streaming_active(self, *, task_id: int, hub: Any) -> None:
        return None

    def set_streaming_inactive(
        self,
        *,
        task_id: int,
        hub: Any,
        warn_on_error: bool = True,
    ) -> None:
        return None

    async def publish_turn_result_events(
        self,
        *,
        hub: Any,
        task_id: int,
        result: Any,
        turn_sequence: int | None,
    ) -> None:
        return None


class _BootstrapService:
    def resolve_start_turn_identity(
        self,
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], int, str, int]:
        return {}, 3, "turn-3", 3


class _WaitingTransitionService:
    def __init__(self, *, waiting: bool) -> None:
        self._waiting = waiting

    def handle_start_interruption(self, **kwargs: Any) -> tuple[bool, int | None]:
        return self._waiting, kwargs.get("reserved_message_id")


class _ResultService:
    def extract_final_content(self, *, result: Any, failure_message: str) -> str:
        return result.final_text or failure_message

    def build_start_completion_metadata(
        self,
        *,
        result_metadata: dict[str, Any],
        conversation_id: str,
        anchor_sequence: int | None,
        turn_sequence: int | None,
    ) -> tuple[dict[str, Any], int | None, int | None]:
        return dict(result_metadata), None, turn_sequence


class _Service:
    def __init__(self, *, waiting: bool = False) -> None:
        self._turn_stream_publisher = _TurnStreamPublisher()
        self._bootstrap_service = _BootstrapService()
        self._waiting_transition_service = _WaitingTransitionService(waiting=waiting)
        self._result_service = _ResultService()
        self.finalized_results: list[dict[str, Any]] = []

    def _context_window_handoff_fields(self, _metadata: Any) -> dict[str, Any]:
        return {}

    def _compression_handoff_fields(self, _metadata: Any) -> dict[str, Any]:
        return {}

    def _extract_and_emit_context_window_metadata(
        self,
        *,
        task_id: int,
        metadata: Any,
        fallback_conversation_id: str,
    ) -> None:
        return None

    def _emit_context_window_event(self, task_id: int, metadata: Any) -> None:
        return None

    async def _finalize_successful_turn_result(self, **kwargs: Any) -> None:
        self.finalized_results.append(dict(kwargs))


class _Facade:
    async def handle_turn(
        self,
        *_args: Any,
        **_kwargs: Any,
    ) -> LangGraphChatResult:
        return LangGraphChatResult(
            final_text="done",
            conversation_id="conv-1",
            metadata={"role": "assistant", "streaming": False},
        )


@pytest.mark.asyncio
async def test_main_owner_cleanup_helper_closes_only_terminal_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_service = _ShellCleanupService()
    monkeypatch.setattr(
        orchestration_module,
        "get_shell_session_service",
        lambda: shell_service,
    )

    for run_status in ("completed", "failed", "cancelled", "declined"):
        await _close_main_owner_shell_sessions_for_status(
            tenant_id=44,
            task_id=9,
            turn_id="task-9-turn-1",
            run_status=run_status,
        )

    await _close_main_owner_shell_sessions_for_status(
        tenant_id=44,
        task_id=9,
        turn_id="task-9-turn-1",
        run_status="waiting_for_human",
    )

    assert shell_service.close_calls == [
        {
            "tenant_id": 44,
            "task_id": 9,
            "execution_owner_id": "main:task-9-turn-1",
        },
        {
            "tenant_id": 44,
            "task_id": 9,
            "execution_owner_id": "main:task-9-turn-1",
        },
        {
            "tenant_id": 44,
            "task_id": 9,
            "execution_owner_id": "main:task-9-turn-1",
        },
        {
            "tenant_id": 44,
            "task_id": 9,
            "execution_owner_id": "main:task-9-turn-1",
        },
    ]


@pytest.mark.asyncio
async def test_main_owner_cleanup_helper_logs_and_swallows_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        orchestration_module,
        "get_shell_session_service",
        lambda: _FailingShellCleanupService(),
    )

    await _close_main_owner_shell_sessions_for_status(
        tenant_id=44,
        task_id=9,
        turn_id="task-9-turn-1",
        run_status="completed",
    )

    assert "shell_session.main_owner_cleanup_failed" in caplog.text


@pytest.mark.asyncio
async def test_start_turn_terminal_completion_closes_main_owner_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_service = _ShellCleanupService()
    lifecycle = _Lifecycle()
    monkeypatch.setattr(
        orchestration_module,
        "get_shell_session_service",
        lambda: shell_service,
    )
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _Hub(),
    )
    monkeypatch.setattr(
        orchestration_module,
        "get_run_lifecycle_service",
        lambda: lifecycle,
    )

    await TurnExecutionOrchestrator().start_turn_generation(
        service=_Service(),
        task_id=9,
        user_id=7,
        tenant_id=44,
        provider="openai",
        model="gpt-test",
        runtime_selection=None,
        runtime_services=SimpleNamespace(),
        message="hello",
        conversation_id="conv-1",
        history=[],
        reserved_message_id=101,
        facade_class=_Facade,
        compression_required_failed_error_code="compression_required_failed",
        retryable_post_tool_error_message="[Error] retry.",
        generation_failed_error_message="[Error] failed.",
    )

    assert shell_service.close_calls == [
        {
            "tenant_id": 44,
            "task_id": 9,
            "execution_owner_id": "main:turn-3",
        }
    ]
    assert lifecycle.end_calls == [
        {"task_id": 9, "turn_id": "turn-3", "status": "completed"}
    ]


@pytest.mark.asyncio
async def test_start_turn_waiting_for_human_preserves_main_owner_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shell_service = _ShellCleanupService()
    lifecycle = _Lifecycle()
    monkeypatch.setattr(
        orchestration_module,
        "get_shell_session_service",
        lambda: shell_service,
    )
    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        lambda: _Hub(),
    )
    monkeypatch.setattr(
        orchestration_module,
        "get_run_lifecycle_service",
        lambda: lifecycle,
    )

    await TurnExecutionOrchestrator().start_turn_generation(
        service=_Service(waiting=True),
        task_id=9,
        user_id=7,
        tenant_id=44,
        provider="openai",
        model="gpt-test",
        runtime_selection=None,
        runtime_services=SimpleNamespace(),
        message="hello",
        conversation_id="conv-1",
        history=[],
        reserved_message_id=101,
        facade_class=_Facade,
        compression_required_failed_error_code="compression_required_failed",
        retryable_post_tool_error_message="[Error] retry.",
        generation_failed_error_message="[Error] failed.",
    )

    assert shell_service.close_calls == []
    assert lifecycle.end_calls == [
        {"task_id": 9, "turn_id": "turn-3", "status": "waiting_for_human"}
    ]
