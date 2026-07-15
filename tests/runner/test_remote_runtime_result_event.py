"""Tests for runner remote-runtime result event classification."""

from __future__ import annotations

from drowai_runner.control_channel.runtime.models import _RemoteRuntimeRequestContext
from drowai_runner.control_channel.runtime.result_event import RemoteRuntimeResultEventBuilder
from runtime_shared.runner_protocol import (
    RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
    RunnerEnvelope,
    RunnerMessageType,
    RunnerRuntimeOperationPayload,
)


def _envelope(message_type: RunnerMessageType) -> RunnerEnvelope:
    return RunnerEnvelope(
        message_id="msg-1",
        message_type=message_type,
        schema_version=RUNNER_PROTOCOL_REMOTE_RUNTIME_VERSION,
        tenant_id="1",
        runner_id="runner-1",
        correlation_id=None,
        runtime_job_id="runtime-job-1",
        task_id=101,
        created_at="2026-06-27T10:00:00+00:00",
        payload=RunnerRuntimeOperationPayload(
            operation_id="operation-1",
            workspace_id="task-101",
            runtime_image="runtime:latest",
            operation=message_type.value,
            params={},
        ),
        raw_message_type=message_type.value,
    )


def _builder() -> RemoteRuntimeResultEventBuilder:
    return RemoteRuntimeResultEventBuilder(frame_lifecycle=object())  # type: ignore[arg-type]


def _context() -> _RemoteRuntimeRequestContext:
    return _RemoteRuntimeRequestContext(
        runtime_job_id="runtime-job-1",
        task_id=101,
        workspace_id="task-101",
    )


def test_failed_vpn_retry_stays_vpn_retry_event() -> None:
    event_type, payload = _builder().build_result_event(
        inbound=_envelope(RunnerMessageType.RUNTIME_VPN_RETRY),
        response={
            "accepted": False,
            "status": "failed",
            "error_code": "RUNNER_VPN_COMMAND_FAILED",
            "error_message": "VPN command exited with non-zero status.",
            "metadata": {},
        },
        context=_context(),
    )

    assert event_type is RunnerMessageType.RUNTIME_VPN_RETRY
    assert payload["status"] == "failed"
    assert payload["error_code"] == "RUNNER_VPN_COMMAND_FAILED"


def test_failed_task_start_still_emits_runtime_failed() -> None:
    event_type, payload = _builder().build_result_event(
        inbound=_envelope(RunnerMessageType.TASK_START),
        response={
            "accepted": False,
            "status": "failed",
            "error_code": "RUNNER_MATERIALIZE_FAILED",
            "error_message": "Container failed.",
            "metadata": {},
        },
        context=_context(),
    )

    assert event_type is RunnerMessageType.RUNTIME_FAILED
    assert payload["status"] == "failed"
    assert payload["error_code"] == "RUNNER_MATERIALIZE_FAILED"
