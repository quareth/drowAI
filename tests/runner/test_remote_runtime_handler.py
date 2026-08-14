"""Tests for runner remote-runtime acknowledgement ordering."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from drowai_runner.control_channel.identity.models import CloudChannelIdentity
from drowai_runner.control_channel.runtime.handler import RemoteRuntimeHandler
from drowai_runner.control_channel.runtime.models import _RemoteRuntimeRequestContext
from drowai_runner.control_channel.session.state import ConnectionSessionState
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
    RunnerEnvelope,
    RunnerMessageType,
)


def test_stream_input_ack_is_sent_after_runner_applies_input() -> None:
    events: list[str] = []

    class _WebSocket:
        def send(self, payload: str) -> None:
            ack = json.loads(payload)
            assert ack["payload"]["status"] == "accepted"
            events.append("ack")

    class _TerminalStreamHandler:
        @staticmethod
        def is_request(_inbound: RunnerEnvelope) -> bool:
            return True

        @staticmethod
        def execute(**_kwargs):
            events.append("input_applied")
            return "succeeded", None

    context = _RemoteRuntimeRequestContext(
        runtime_job_id="runtime-1",
        task_id=106,
        workspace_id="task-106",
    )
    handler = RemoteRuntimeHandler(
        validator=SimpleNamespace(
            validate=lambda **_kwargs: ("accepted", None, context)
        ),
        result_event_builder=SimpleNamespace(),
        terminal_stream_handler=_TerminalStreamHandler(),
        frame_lifecycle=SimpleNamespace(),
        operation_service_provider=lambda: SimpleNamespace(),
        active_terminal_sessions={},
    )
    envelope = RunnerEnvelope(
        message_id="terminal-stream-input-1",
        message_type=RunnerMessageType.TERMINAL_INPUT,
        schema_version=RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
        tenant_id="1",
        runner_id="runner-1",
        correlation_id=None,
        runtime_job_id="runtime-1",
        task_id=106,
        created_at=datetime.now(tz=UTC).isoformat(),
        payload={"params": {"stream_mode": True}},
        raw_message_type=RunnerMessageType.TERMINAL_INPUT.value,
    )

    handler.handle(
        websocket=_WebSocket(),
        identity=CloudChannelIdentity(
            tenant_id=1,
            runner_id="runner-1",
            credential_secret="<KEY_SET>",
            channel_endpoint="ws://runner.test",
            protocol_version=RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
            heartbeat_interval_seconds=30,
        ),
        inbound=envelope,
        session_state=ConnectionSessionState(),
    )

    assert events == ["input_applied", "ack"]
