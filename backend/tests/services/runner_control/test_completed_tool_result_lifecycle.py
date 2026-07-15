"""Tests for deferred terminalization of completed runner tool.result frames."""

from __future__ import annotations

from unittest.mock import MagicMock

from backend.services.runner_control.message_ingest import _runtime_job_transition_status_for_envelope
from backend.services.runner_control.runtime_event_service import (
    RuntimeEventService,
    _resolve_runtime_job_status,
)
from runtime_shared.runner_protocol import (
    RUNNER_TOOL_RESULT_COMPLETED_STATUS,
    RunnerEnvelope,
    RunnerMessageType,
    RunnerToolResultPayload,
    is_completed_process_tool_result_status,
)


def _tool_result_envelope(*, status: str, success: bool, exit_code: int = 0) -> RunnerEnvelope:
    payload = RunnerToolResultPayload(
        operation_id="op-1",
        command_id="cmd-1",
        tool="information_gathering.network_discovery.fping",
        status=status,
        success=success,
        exit_code=exit_code,
        stdout="alive hosts",
        stderr="",
        artifacts=(),
        error_code=None,
        error_message=None,
        result={},
        metadata={
            "task_runtime_job_id": "task-start-1",
            "command_id": "cmd-1",
            "workspace_id": "task-60",
        },
    )
    return RunnerEnvelope(
        message_id="msg-1",
        message_type=RunnerMessageType.TOOL_RESULT,
        schema_version="tooling_plane.v1",
        tenant_id="1",
        runner_id="runner-1",
        correlation_id="corr-1",
        runtime_job_id="tool-command-job-1",
        task_id=60,
        created_at="2026-05-29T12:00:00+00:00",
        payload=payload,
        raw_message_type=RunnerMessageType.TOOL_RESULT.value,
    )


def test_protocol_accepts_completed_status() -> None:
    assert is_completed_process_tool_result_status(RUNNER_TOOL_RESULT_COMPLETED_STATUS)
    envelope = _tool_result_envelope(status=RUNNER_TOOL_RESULT_COMPLETED_STATUS, success=False, exit_code=1)
    assert _resolve_runtime_job_status(envelope=envelope) is None
    assert _runtime_job_transition_status_for_envelope(envelope=envelope) is None


def test_completed_tool_result_skips_terminal_ingest() -> None:
    envelope = _tool_result_envelope(status=RUNNER_TOOL_RESULT_COMPLETED_STATUS, success=False, exit_code=1)
    service = RuntimeEventService(db=MagicMock())
    outcome = service._ingest_tool_result_execution(
        tenant_id=1,
        runtime_job=MagicMock(id="job-1", task_id=60),
        envelope=envelope,
        runtime_job_status=None,
    )
    assert outcome is None


def test_terminal_non_completion_stays_terminal() -> None:
    envelope = _tool_result_envelope(status="timed_out", success=False, exit_code=124)
    assert _resolve_runtime_job_status(envelope=envelope) == "failed"


def test_canonical_succeeded_still_terminalizes() -> None:
    envelope = _tool_result_envelope(status="succeeded", success=True, exit_code=1)
    assert _resolve_runtime_job_status(envelope=envelope) == "succeeded"
