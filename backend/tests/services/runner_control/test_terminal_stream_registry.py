"""Tests for in-memory cloud terminal stream routing."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from backend.services.runner_control.terminal_stream_registry import RunnerTerminalStreamRegistry
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_SCHEMA_VERSION,
    RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
    RunnerAckPayload,
    RunnerEnvelope,
    RunnerMessageType,
)


def test_terminal_stream_registry_routes_known_frames_only() -> None:
    registry = RunnerTerminalStreamRegistry()
    runner_id = uuid4()
    registry.register_stream(
        tenant_id=1,
        runner_id=runner_id,
        task_id=9,
        session_id="sess-1",
    )

    assert registry.append_stream_frame(
        tenant_id=1,
        runner_id=runner_id,
        task_id=9,
        session_id="sess-1",
        data="hello",
    )
    assert not registry.append_stream_frame(
        tenant_id=1,
        runner_id=runner_id,
        task_id=9,
        session_id="unknown",
        data="lost",
    )

    data = asyncio.run(
        registry.read_stream_output(
            tenant_id=1,
            runner_id=runner_id,
            task_id=9,
            session_id="sess-1",
            size=1024,
            timeout=0,
        )
    )

    assert data == b"hello"


def test_terminal_stream_registry_pushes_known_frames_to_registered_sink() -> None:
    registry = RunnerTerminalStreamRegistry()
    runner_id = uuid4()
    pushed: list[dict[str, object]] = []

    async def _sink(**kwargs) -> bool:
        pushed.append(dict(kwargs))
        return True

    registry.register_stream(
        tenant_id=1,
        runner_id=runner_id,
        task_id=9,
        session_id="sess-1",
    )
    registry.register_frame_sink(_sink)

    assert asyncio.run(
        registry.ingest_stream_frame(
            tenant_id=1,
            runner_id=runner_id,
            task_id=9,
            session_id="sess-1",
            data="hello",
        )
    )
    assert not asyncio.run(
        registry.ingest_stream_frame(
            tenant_id=1,
            runner_id=runner_id,
            task_id=9,
            session_id="missing",
            data="lost",
        )
    )

    assert pushed == [
        {
            "tenant_id": 1,
            "runner_id": runner_id,
            "task_id": 9,
            "provider_session_id": "sess-1",
            "data": b"hello",
        }
    ]


def test_terminal_stream_registry_consumes_stream_ack_without_persistence_path() -> None:
    registry = RunnerTerminalStreamRegistry()
    envelope = RunnerEnvelope(
        message_id="ack-1",
        message_type=RunnerMessageType.RUNNER_ACK,
        schema_version=RUNNER_PROTOCOL_SCHEMA_VERSION,
        tenant_id="1",
        runner_id=str(uuid4()),
        correlation_id=None,
        runtime_job_id=None,
        task_id=None,
        created_at=datetime.now(tz=UTC).isoformat(),
        payload=RunnerAckPayload(
            acked_message_id="terminal-stream-abc",
            status="accepted",
            error_code=None,
        ),
        raw_message_type=RunnerMessageType.RUNNER_ACK.value,
    )

    assert registry.handle_stream_ack(envelope)


def test_terminal_stream_registry_sends_non_durable_envelope() -> None:
    registry = RunnerTerminalStreamRegistry()
    runner_id = uuid4()
    sent: list[RunnerEnvelope] = []

    async def _send(envelope: RunnerEnvelope) -> None:
        sent.append(envelope)

    assert not registry.has_channel(tenant_id=1, runner_id=runner_id)
    registry.register_channel(tenant_id=1, runner_id=runner_id, sender=_send)
    assert registry.has_channel(tenant_id=1, runner_id=runner_id)
    envelope = RunnerEnvelope(
        message_id="terminal-stream-send",
        message_type=RunnerMessageType.TERMINAL_INPUT,
        schema_version=RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
        tenant_id="1",
        runner_id=str(runner_id),
        correlation_id=None,
        runtime_job_id="runtime-1",
        task_id=9,
        created_at=datetime.now(tz=UTC).isoformat(),
        payload={
            "runtime_job_id": "runtime-1",
            "operation_id": "terminal.input:test",
            "workspace_id": "task-9",
            "runtime_image": "image",
            "operation": "terminal.input",
            "params": {
                "runtime_job_id": "runtime-1",
                "session_id": "sess-1",
                "data": "x",
                "stream_mode": True,
            },
        },
        raw_message_type=RunnerMessageType.TERMINAL_INPUT.value,
    )

    asyncio.run(registry.send_stream_envelope(tenant_id=1, runner_id=runner_id, envelope=envelope))

    assert sent == [envelope]
    registry.unregister_channel(tenant_id=1, runner_id=runner_id)
    assert not registry.has_channel(tenant_id=1, runner_id=runner_id)
