"""Tool-command result projection for cloud runner dispatch.

This module owns provider-result projection for tool.command runtime jobs. It
does not wait for results, terminalize runtime jobs, enqueue commands, finalize
tool results, or import the provider facade.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.models.runner_control import RuntimeJob
from backend.services.runtime_provider.contracts import (
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    build_runtime_result,
)
from runtime_shared.runner_protocol import is_completed_process_tool_result_status

from ..error_codes import (
    _RUNNER_ACK_FAILED,
    _RUNNER_TOOL_RESULT_CANCELLED,
)
from ..normalization import (
    _coerce_int_or_default,
    _resolve_optional_text,
)


class ToolCommandResultProjector:
    """Builds provider results from tool.command runtime jobs."""

    def __init__(self, *, provider_name: str) -> None:
        self._provider_name = provider_name

    def build_tool_command_availability_result(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        runtime_job: RuntimeJob,
        metadata: Mapping[str, Any],
        command_id: str,
        tool: str,
    ) -> RuntimeOperationResult:
        runtime_job_status = str(runtime_job.status or "").strip().lower()
        result_json = runtime_job.result_json if isinstance(runtime_job.result_json, Mapping) else {}
        delegate_result = self.project_delegate_result(
            runtime_job_status=runtime_job_status,
            runtime_job_error_code=runtime_job.error_code,
            runtime_job_error_message=runtime_job.error_message,
            raw_result=result_json,
            command_id=command_id,
            tool=tool,
        )
        availability_metadata = dict(metadata)
        availability_metadata["runtime_job_status"] = runtime_job_status
        availability_metadata["delegate_result"] = delegate_result
        availability_metadata["result_availability"] = "process_completed"
        return build_runtime_result(
            request,
            accepted=True,
            provider=self._provider_name,
            status=RuntimeOperationStatus.SUCCEEDED,
            metadata=availability_metadata,
        )

    def build_tool_command_terminal_result(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        runtime_job: RuntimeJob,
        metadata: Mapping[str, Any],
        command_id: str,
        tool: str,
    ) -> RuntimeOperationResult:
        runtime_job_status = str(runtime_job.status or "").strip().lower()
        result_json = runtime_job.result_json if isinstance(runtime_job.result_json, Mapping) else {}
        delegate_result = self.project_delegate_result(
            runtime_job_status=runtime_job_status,
            runtime_job_error_code=runtime_job.error_code,
            runtime_job_error_message=runtime_job.error_message,
            raw_result=result_json,
            command_id=command_id,
            tool=tool,
        )
        terminal_metadata = dict(metadata)
        terminal_metadata["runtime_job_status"] = runtime_job_status
        terminal_metadata["delegate_result"] = delegate_result
        if result_json and str(result_json.get("source") or "") == "runner_ack":
            terminal_metadata["runner_ack"] = dict(result_json)

        if runtime_job_status == "succeeded":
            return build_runtime_result(
                request,
                accepted=True,
                provider=self._provider_name,
                status=RuntimeOperationStatus.SUCCEEDED,
                metadata=terminal_metadata,
            )

        default_error_code = "RUNNER_TOOL_COMMAND_FAILED"
        if runtime_job_status == "cancelled":
            default_error_code = _RUNNER_TOOL_RESULT_CANCELLED
        elif runtime_job_status in {"lost", "expired"}:
            default_error_code = f"RUNNER_TOOL_COMMAND_{runtime_job_status.upper()}"

        error_code = str(
            runtime_job.error_code
            or delegate_result.get("error_code")
            or default_error_code
        )
        error_message = str(
            runtime_job.error_message
            or delegate_result.get("error_message")
            or "Runner tool.command runtime job failed before tool result projection."
        )
        return build_runtime_result(
            request,
            accepted=False,
            provider=self._provider_name,
            status=RuntimeOperationStatus.FAILED,
            error_code=error_code,
            error_message=error_message,
            metadata=terminal_metadata,
        )

    def project_delegate_result(
        self,
        *,
        runtime_job_status: str,
        runtime_job_error_code: object,
        runtime_job_error_message: object,
        raw_result: object,
        command_id: str,
        tool: str,
    ) -> dict[str, Any]:
        result_json = raw_result if isinstance(raw_result, Mapping) else {}
        source = str(result_json.get("source") or "").strip().lower()
        raw_result_payload = result_json.get("result")
        result_payload = (
            {str(key): value for key, value in raw_result_payload.items()}
            if isinstance(raw_result_payload, Mapping)
            else {}
        )
        raw_metadata = result_json.get("metadata")
        metadata_payload = (
            {str(key): value for key, value in raw_metadata.items()}
            if isinstance(raw_metadata, Mapping)
            else {}
        )

        success = runtime_job_status == "succeeded"
        status = str(result_json.get("status") or ("succeeded" if success else "failed")).strip().lower()
        if is_completed_process_tool_result_status(status):
            success = bool(result_json.get("success"))
        elif status == "succeeded":
            success = True
        elif status in {"failed", "timed_out", "cancelled", "canceled"}:
            success = False
        exit_code_default = 0 if success else 2
        exit_code = _coerce_int_or_default(result_json.get("exit_code"), default=exit_code_default)
        stdout = str(result_json.get("stdout") or "")
        stderr = str(result_json.get("stderr") or "")
        artifacts_value = result_json.get("artifacts")
        artifacts = [str(item) for item in artifacts_value] if isinstance(artifacts_value, list) else []
        error_code = _resolve_optional_text(
            runtime_job_error_code
            or result_json.get("error_code")
            or metadata_payload.get("error_code")
        )
        error_message = _resolve_optional_text(
            runtime_job_error_message
            or result_json.get("error_message")
            or stderr
        )

        if source == "runner_ack" and not result_payload:
            ack_status = str(result_json.get("ack_status") or "").strip().lower()
            error_code = error_code or _RUNNER_ACK_FAILED
            error_message = error_message or "Runner rejected tool.command delivery."
            stderr = stderr or error_message
            status = "failed"
            success = False
            exit_code = 2
            metadata_payload = {**metadata_payload, "source": "runner_ack", "ack_status": ack_status}

        if runtime_job_status == "cancelled":
            status = "cancelled"
            success = False
            error_code = error_code or _RUNNER_TOOL_RESULT_CANCELLED
            error_message = error_message or "Tool command result waiter was cancelled."

        if runtime_job_status in {"lost", "expired"} and error_code is None:
            error_code = f"RUNNER_TOOL_COMMAND_{runtime_job_status.upper()}"

        if not success and error_message and not stderr:
            stderr = error_message

        return {
            "command_id": command_id,
            "tool": tool,
            "status": status,
            "success": success,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "artifacts": artifacts,
            "error_code": error_code,
            "error_message": error_message,
            "result": result_payload,
            "metadata": metadata_payload,
            "operation_id": _resolve_optional_text(result_json.get("operation_id")),
        }


def tool_command_process_result_available(result_json: Mapping[str, Any]) -> bool:
    """Return whether a non-terminal runtime job has a completed process result."""
    status = str(result_json.get("status") or "").strip().lower()
    if is_completed_process_tool_result_status(status):
        return True
    if status in {"failed", "timed_out", "cancelled", "canceled"}:
        return True
    if status == "succeeded":
        return True
    return False


def has_pending_artifact_promotion(metadata: object) -> bool:
    """Return whether tool-result metadata still needs artifact promotion."""
    if not isinstance(metadata, Mapping):
        return False
    upload = metadata.get("artifact_upload")
    if isinstance(upload, Mapping):
        upload_status = str(upload.get("status") or "").strip().lower()
        if upload_status in {"pending", "upload_pending"}:
            return True
        if upload_status:
            return False
    promotion = metadata.get("artifact_promotion")
    if isinstance(promotion, Mapping):
        promotion_status = str(promotion.get("status") or "").strip().lower()
    else:
        promotion_status = str(metadata.get("artifact_promotion_status") or "").strip().lower()
    if promotion_status in {"pending", "upload_pending"}:
        return True
    manifest = metadata.get("artifact_manifest")
    if isinstance(manifest, Mapping):
        return str(manifest.get("status") or "").strip().lower() == "ready_for_upload_request"
    return False
