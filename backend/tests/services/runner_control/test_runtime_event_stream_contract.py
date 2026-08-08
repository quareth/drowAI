"""Tests that runner runtime events stay out of the chat stream contract."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import Mock

import pytest

from backend.services.runner_control.runtime_event_service import RuntimeEventService
from backend.services.runner_control.terminal_stream_registry import (
    get_runner_terminal_stream_registry,
)
from runtime_shared.runner_protocol import (
    RunnerEnvelope,
    RunnerMessageType,
    RunnerRuntimeOperationResultPayload,
    RunnerTerminalFramePayload,
)


@pytest.mark.asyncio
async def test_runner_runtime_event_does_not_publish_to_chat_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_stream_hub_lookup():
        raise AssertionError("runner runtime events must not enter the chat stream hub")

    monkeypatch.setattr(
        "backend.services.streaming.in_memory_hub.get_in_memory_stream_hub",
        _fail_stream_hub_lookup,
    )

    runner_id = uuid.uuid4()
    envelope = RunnerEnvelope(
        message_id="msg-runtime-started",
        message_type=RunnerMessageType.RUNTIME_STARTED,
        schema_version="remote_runtime.v1",
        tenant_id="1",
        runner_id=str(runner_id),
        correlation_id="corr-runtime-started",
        runtime_job_id=str(uuid.uuid4()),
        task_id=7,
        created_at="2026-05-26T13:00:00+00:00",
        payload=RunnerRuntimeOperationResultPayload(
            operation_id="op-runtime-started",
            status="succeeded",
            error_code=None,
            error_message=None,
            result={"workspace_id": "task-7"},
        ),
        raw_message_type="runtime.started",
    )

    service = RuntimeEventService(db=Mock(), object_store=Mock())
    service._publish_runtime_event(
        tenant_id=1,
        runner_id=runner_id,
        task_id=7,
        envelope=envelope,
        task_status=None,
        tool_result_promotion=None,
    )
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_runner_terminal_frame_reaches_registered_live_stream() -> None:
    runner_id = uuid.uuid4()
    registry = get_runner_terminal_stream_registry()
    registry.register_stream(
        tenant_id=1,
        runner_id=runner_id,
        task_id=7,
        session_id="terminal-session-1",
    )
    envelope = RunnerEnvelope(
        message_id="msg-terminal-frame",
        message_type=RunnerMessageType.TERMINAL_FRAME,
        schema_version="remote_runtime.v1",
        tenant_id="1",
        runner_id=str(runner_id),
        correlation_id=None,
        runtime_job_id=str(uuid.uuid4()),
        task_id=7,
        created_at="2026-05-26T13:00:00+00:00",
        payload=RunnerTerminalFramePayload(
            session_id="terminal-session-1",
            sequence=1,
            stream="stdout",
            data="shell output",
        ),
        raw_message_type="terminal.frame",
    )

    try:
        RuntimeEventService(db=Mock(), object_store=Mock())._publish_runtime_event(
            tenant_id=1,
            runner_id=runner_id,
            task_id=7,
            envelope=envelope,
            task_status=None,
            tool_result_promotion=None,
        )

        output = await registry.read_stream_output(
            tenant_id=1,
            runner_id=runner_id,
            task_id=7,
            session_id="terminal-session-1",
            size=1024,
            timeout=0,
        )
    finally:
        registry.unregister_stream(
            tenant_id=1,
            runner_id=runner_id,
            task_id=7,
            session_id="terminal-session-1",
        )

    assert output == b"shell output"
