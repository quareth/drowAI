"""Tests for in-memory cloud terminal stream routing."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from backend.services.runner_control import terminal_stream_registry as stream_registry_module
from backend.services.runner_control.terminal_stream_registry import RunnerTerminalStreamRegistry
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_SCHEMA_VERSION,
    RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
    RunnerAckPayload,
    RunnerEnvelope,
    RunnerMessageType,
)


def _register_authorized_stream(
    registry: RunnerTerminalStreamRegistry,
    *,
    tenant_id: int,
    runner_id: UUID,
    task_id: int,
    session_id: str,
) -> None:
    registry.authorize_stream(
        tenant_id=tenant_id,
        runner_id=runner_id,
        task_id=task_id,
        session_id=session_id,
    )
    assert registry.register_stream(
        tenant_id=tenant_id,
        runner_id=runner_id,
        task_id=task_id,
        session_id=session_id,
    )


@pytest.mark.asyncio
async def test_stream_reader_drains_data_before_structured_exec_exit() -> None:
    registry = RunnerTerminalStreamRegistry()
    runner_id = uuid4()
    _register_authorized_stream(
        registry,
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="session-exit",
    )
    assert registry.append_stream_frame(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="session-exit",
        data="tail\n",
    )
    assert registry.append_stream_frame(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="session-exit",
        data="",
        eof=True,
        process_status="completed",
        exit_code=0,
    )

    output = await registry.read_stream_output_result(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="session-exit",
        size=4096,
        timeout=0,
    )
    terminal = await registry.read_stream_output_result(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="session-exit",
        size=4096,
        timeout=0,
    )

    assert output.data == b"tail\n"
    assert output.eof is False
    assert terminal.eof is True
    assert terminal.process_status == "completed"
    assert terminal.exit_code == 0


@pytest.mark.asyncio
async def test_stream_frames_arriving_before_consumer_registration_are_retained() -> None:
    registry = RunnerTerminalStreamRegistry()
    runner_id = uuid4()

    async def _sender(_envelope: RunnerEnvelope) -> None:
        return None

    registry.register_channel(
        tenant_id=1,
        runner_id=runner_id,
        connection_id="conn-early-frame",
        sender=_sender,
    )
    registry.authorize_stream(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="session-early-frame",
    )
    assert registry.append_stream_frame(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="session-early-frame",
        data="fast output\n",
    )
    assert registry.append_stream_frame(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="session-early-frame",
        data="",
        eof=True,
        process_status="completed",
        exit_code=0,
    )

    assert registry.register_stream(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="session-early-frame",
    )
    output = await registry.read_stream_output_result(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="session-early-frame",
        size=4096,
        timeout=0,
    )
    terminal = await registry.read_stream_output_result(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="session-early-frame",
        size=4096,
        timeout=0,
    )

    assert output.data == b"fast output\n"
    assert terminal.eof is True
    assert terminal.process_status == "completed"
    assert terminal.exit_code == 0


def test_connected_runner_cannot_allocate_unknown_stream_buffers() -> None:
    registry = RunnerTerminalStreamRegistry()
    runner_id = uuid4()

    async def _sender(_envelope: RunnerEnvelope) -> None:
        return None

    registry.register_channel(
        tenant_id=1,
        runner_id=runner_id,
        connection_id="conn-untrusted-frames",
        sender=_sender,
    )
    assert not registry.register_stream(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="unknown-consumer",
    )

    for index in range(100):
        assert not registry.append_stream_frame(
            tenant_id=1,
            runner_id=runner_id,
            task_id=2,
            session_id=f"unknown-{index}",
            data="x" * 1024,
        )

    assert registry._buffers == {}


def test_late_frame_cannot_recreate_unregistered_stream() -> None:
    registry = RunnerTerminalStreamRegistry()
    runner_id = uuid4()

    async def _sender(_envelope: RunnerEnvelope) -> None:
        return None

    registry.register_channel(
        tenant_id=1,
        runner_id=runner_id,
        connection_id="conn-late-frame",
        sender=_sender,
    )
    _register_authorized_stream(
        registry,
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="closed-session",
    )
    registry.unregister_stream(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="closed-session",
    )

    assert not registry.append_stream_frame(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="closed-session",
        data="late output",
    )
    assert registry._buffers == {}


def test_task_cleanup_revokes_pending_and_registered_streams_only_for_task() -> None:
    registry = RunnerTerminalStreamRegistry()
    runner_id = uuid4()
    for task_id, session_id in ((2, "pending"), (2, "registered"), (3, "other")):
        registry.authorize_stream(
            tenant_id=1,
            runner_id=runner_id,
            task_id=task_id,
            session_id=session_id,
        )
    assert registry.register_stream(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="registered",
    )

    registry.clear_task(tenant_id=1, task_id=2)

    assert not registry.append_stream_frame(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="pending",
        data="late pending output",
    )
    assert not registry.append_stream_frame(
        tenant_id=1,
        runner_id=runner_id,
        task_id=2,
        session_id="registered",
        data="late registered output",
    )
    assert registry.append_stream_frame(
        tenant_id=1,
        runner_id=runner_id,
        task_id=3,
        session_id="other",
        data="still active",
    )


def test_stale_channel_unregister_cannot_remove_replacement_route() -> None:
    registry = RunnerTerminalStreamRegistry()
    runner_id = uuid4()

    async def _old_sender(_envelope: RunnerEnvelope) -> None:
        return None

    async def _new_sender(_envelope: RunnerEnvelope) -> None:
        return None

    registry.register_channel(
        tenant_id=1,
        runner_id=runner_id,
        connection_id="conn-old",
        sender=_old_sender,
    )
    registry.register_channel(
        tenant_id=1,
        runner_id=runner_id,
        connection_id="conn-new",
        sender=_new_sender,
    )
    registry.unregister_channel(
        tenant_id=1,
        runner_id=runner_id,
        connection_id="conn-old",
    )

    assert registry.is_current_channel(
        tenant_id=1,
        runner_id=runner_id,
        connection_id="conn-new",
    )


def test_terminal_stream_registry_routes_known_frames_only() -> None:
    registry = RunnerTerminalStreamRegistry()
    runner_id = uuid4()
    _register_authorized_stream(
        registry,
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


def test_terminal_stream_registry_reports_bounded_buffer_loss_once(
    monkeypatch,
) -> None:
    monkeypatch.setattr(stream_registry_module, "_MAX_BUFFER_BYTES", 8)
    registry = RunnerTerminalStreamRegistry()
    runner_id = uuid4()
    _register_authorized_stream(
        registry,
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
        data="first",
    )
    assert registry.append_stream_frame(
        tenant_id=1,
        runner_id=runner_id,
        task_id=9,
        session_id="sess-1",
        data="second",
    )

    first_read = asyncio.run(
        registry.read_stream_output_result(
            tenant_id=1,
            runner_id=runner_id,
            task_id=9,
            session_id="sess-1",
            size=3,
            timeout=0,
        )
    )
    second_read = asyncio.run(
        registry.read_stream_output_result(
            tenant_id=1,
            runner_id=runner_id,
            task_id=9,
            session_id="sess-1",
            size=8,
            timeout=0,
        )
    )

    assert first_read.ok is True
    assert first_read.data == b"sec"
    assert first_read.truncated is True
    assert second_read.data == b"ond"
    assert second_read.truncated is False


def test_terminal_stream_registry_reports_loss_when_no_frame_bytes_remain(
    monkeypatch,
) -> None:
    monkeypatch.setattr(stream_registry_module, "_MAX_BUFFER_BYTES", 2)
    registry = RunnerTerminalStreamRegistry()
    runner_id = uuid4()
    _register_authorized_stream(
        registry,
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
        data="dropped",
    )

    result = asyncio.run(
        registry.read_stream_output_result(
            tenant_id=1,
            runner_id=runner_id,
            task_id=9,
            session_id="sess-1",
            size=8,
            timeout=0,
        )
    )

    assert result.ok is True
    assert result.data == b""
    assert result.truncated is True


def test_terminal_stream_registry_ingest_has_one_buffered_delivery_path() -> None:
    registry = RunnerTerminalStreamRegistry()
    runner_id = uuid4()

    _register_authorized_stream(
        registry,
        tenant_id=1,
        runner_id=runner_id,
        task_id=9,
        session_id="sess-1",
    )
    assert not hasattr(registry, "register_frame_sink")

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

    result = asyncio.run(
        registry.read_stream_output_result(
            tenant_id=1,
            runner_id=runner_id,
            task_id=9,
            session_id="sess-1",
            size=64,
            timeout=0,
        )
    )
    assert result.ok is True
    assert result.data == b"hello"


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
