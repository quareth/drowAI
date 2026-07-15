"""Pure tooling_plane tool-result payload shaping and status normalization.

Converts a runner operation response into the tooling_plane tool-result transport
payload and normalizes its status value only. No command execution, no
transport sends, no websocket/queue/job-store I/O, and no connection-session
state. Never imports ``drowai_runner.cloud_client``.
"""

from __future__ import annotations

from typing import Mapping

from drowai_runner.control_channel.constants import (
    _SEMANTIC_TOOL_METADATA_KEYS,
    _TOOLING_PLANE_TOOL_RESULT_STATUS_COMPLETED,
)
from runtime_shared.runner_protocol import (
    RUNNER_TOOL_RESULT_COMPLETED_STATUS,
    RUNNER_TOOL_RESULT_VALID_STATUSES,
    RunnerEnvelope,
    RunnerToolResultPayload,
    is_completed_process_tool_result_status,
    sanitize_tool_result_payload_for_transport,
)


def _build_tooling_plane_tool_result_payload(
    *,
    inbound: RunnerEnvelope,
    response: Mapping[str, object],
    artifact_metadata_patch: Mapping[str, object] | None = None,
    normalized_artifacts: tuple[str, ...] | None = None,
) -> RunnerToolResultPayload:
    payload = inbound.payload
    metadata = response.get("metadata")
    response_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    accepted = bool(response.get("accepted"))
    raw_status = str(response.get("status") or "failed").strip().lower() or "failed"
    status = _normalize_tooling_plane_tool_result_status(
        raw_status=raw_status,
        accepted=accepted,
    )
    raw_success_value = response_metadata.get("success")
    if is_completed_process_tool_result_status(status):
        if isinstance(raw_success_value, bool):
            success = raw_success_value
        else:
            success = False
    else:
        success = accepted and status == "succeeded"
    exit_code_raw = response_metadata.get("exit_code")
    try:
        exit_code = int(exit_code_raw) if exit_code_raw is not None else (0 if success else 1)
    except (TypeError, ValueError):
        exit_code = 0 if success else 1
    if is_completed_process_tool_result_status(status) and not isinstance(raw_success_value, bool):
        success = exit_code == 0
    stdout = str(response_metadata.get("stdout") or "")
    stderr = str(response_metadata.get("stderr") or "")
    artifacts_raw = response_metadata.get("artifacts")
    artifacts: tuple[str, ...] = ()
    if isinstance(artifacts_raw, (list, tuple)):
        artifacts = tuple(str(item) for item in artifacts_raw if str(item).strip())
    if normalized_artifacts is not None:
        artifacts = normalized_artifacts
    tool_result_metadata = {
        "task_runtime_job_id": str(payload.task_runtime_job_id).strip(),
        "command_id": str(payload.command_id).strip(),
        "workspace_id": str(payload.workspace_id).strip(),
        "command_text": str(payload.command).strip(),
    }
    if is_completed_process_tool_result_status(status):
        tool_result_metadata["process_success"] = success
        tool_result_metadata["process_exit_code"] = exit_code
    runner_tool_metadata = response_metadata.get("metadata")
    if isinstance(runner_tool_metadata, Mapping):
        tool_metadata = dict(runner_tool_metadata)
        tool_result_metadata["tool_metadata"] = tool_metadata
        for semantic_key in _SEMANTIC_TOOL_METADATA_KEYS:
            if semantic_key in tool_metadata:
                tool_result_metadata[semantic_key] = tool_metadata[semantic_key]
    for key, value in response_metadata.items():
        key_text = str(key).strip()
        if key_text in {"runtime_job_id", "command_id", "exit_code", "stdout", "stderr", "artifacts", "metadata"}:
            continue
        tool_result_metadata[key_text] = value
    if artifact_metadata_patch:
        for key, value in artifact_metadata_patch.items():
            key_text = str(key).strip()
            if not key_text:
                continue
            tool_result_metadata[key_text] = value
    transport_payload = sanitize_tool_result_payload_for_transport(
        {
            "operation_id": str(payload.operation_id).strip(),
            "command_id": str(payload.command_id).strip(),
            "tool": str(payload.tool).strip(),
            "status": status,
            "success": success,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "artifacts": artifacts,
            "error_code": (str(response.get("error_code")).strip() or None)
            if response.get("error_code") is not None
            else None,
            "error_message": (str(response.get("error_message")).strip() or None)
            if response.get("error_message") is not None
            else None,
            "result": {},
            "metadata": tool_result_metadata,
        }
    )
    bounded_metadata = transport_payload.get("metadata")
    normalized_metadata = dict(bounded_metadata) if isinstance(bounded_metadata, Mapping) else {}
    return RunnerToolResultPayload(
        operation_id=str(transport_payload.get("operation_id") or str(payload.operation_id).strip()),
        command_id=str(transport_payload.get("command_id") or str(payload.command_id).strip()),
        tool=str(transport_payload.get("tool") or str(payload.tool).strip()),
        status=status,
        success=success,
        exit_code=int(transport_payload.get("exit_code") or exit_code),
        stdout=str(transport_payload.get("stdout") or ""),
        stderr=str(transport_payload.get("stderr") or ""),
        artifacts=artifacts,
        error_code=(str(transport_payload.get("error_code")).strip() or None)
        if transport_payload.get("error_code") is not None
        else None,
        error_message=(str(transport_payload.get("error_message")).strip() or None)
        if transport_payload.get("error_message") is not None
        else None,
        result={},
        metadata=normalized_metadata,
    )


def _normalize_tooling_plane_tool_result_status(*, raw_status: str, accepted: bool) -> str:
    if raw_status == _TOOLING_PLANE_TOOL_RESULT_STATUS_COMPLETED and accepted:
        return RUNNER_TOOL_RESULT_COMPLETED_STATUS
    if raw_status in RUNNER_TOOL_RESULT_VALID_STATUSES:
        return raw_status
    return "failed"
