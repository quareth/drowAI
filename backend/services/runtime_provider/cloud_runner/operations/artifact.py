"""Artifact operation handling for the cloud runner provider.

This module owns remote workspace artifact read, write, query, and promotion
operation bodies. It delegates dispatch and generic result polling to bounded
collaborators and does not import the provider facade or host workspace files.
"""

from __future__ import annotations

from typing import Any

from runtime_shared.runner_protocol import RunnerMessageType
from runtime_shared.workspace_write_mode import (
    WORKSPACE_WRITE_MODE_APPEND,
    normalize_workspace_write_mode,
    workspace_path_allows_append,
)

from ..dispatch.operation_waiter import (
    CloudRunnerOperationWaiter,
    _should_wait_for_operation_result,
)
from ..dispatch.remote_dispatcher import CloudRunnerRemoteDispatcher
from ..normalization import _resolve_optional_int, _resolve_optional_text
from ..result_builders import CloudRunnerResultBuilder
from ...contracts import RuntimeOperationRequest, RuntimeOperationResult


class CloudRunnerArtifactOperations:
    """Handles remote workspace artifact provider operations."""

    def __init__(
        self,
        *,
        remote_dispatcher: CloudRunnerRemoteDispatcher,
        operation_waiter: CloudRunnerOperationWaiter,
        result_builder: CloudRunnerResultBuilder,
    ) -> None:
        self._remote_dispatcher = remote_dispatcher
        self._operation_waiter = operation_waiter
        self._result_builder = result_builder

    async def read_runtime_artifact_file(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        operation_name = "read_runtime_artifact_file"
        artifact_path = _resolve_optional_text(
            request.payload.get("artifact_path")
            or request.payload.get("path")
            or request.payload.get("file_path")
        )
        if artifact_path is None:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`path` is required for read_runtime_artifact_file.",
            )
        params: dict[str, Any] = {
            "artifact_path": artifact_path,
            "binary": bool(request.payload.get("binary", False)),
            "encoding": str(request.payload.get("encoding") or "utf-8"),
        }
        max_bytes = _resolve_optional_int(request.payload.get("max_bytes"))
        max_chars = _resolve_optional_int(request.payload.get("max_chars"))
        if max_bytes is not None:
            params["max_bytes"] = max_bytes
        if max_chars is not None:
            params["max_chars"] = max_chars
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name=operation_name,
            message_type=RunnerMessageType.RUNTIME_WORKSPACE_READ,
            params=params,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name=operation_name,
            expected_message_type=RunnerMessageType.RUNTIME_WORKSPACE_READ,
        )

    async def write_runtime_artifact_file(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        operation_name = "write_runtime_artifact_file"
        artifact_path = _resolve_optional_text(
            request.payload.get("artifact_path")
            or request.payload.get("path")
            or request.payload.get("file_path")
        )
        if artifact_path is None:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`path` is required for write_runtime_artifact_file.",
            )
        content_base64 = _resolve_optional_text(request.payload.get("content_base64"))
        content = _resolve_optional_text(request.payload.get("content"))
        if content_base64 is None and content is None:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`content_base64` or `content` is required for write_runtime_artifact_file.",
            )
        mode = normalize_workspace_write_mode(request.payload.get("mode"))
        if mode is None:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`mode` must be `write` or `append` for write_runtime_artifact_file.",
            )
        if mode == WORKSPACE_WRITE_MODE_APPEND and not workspace_path_allows_append(artifact_path):
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`mode=append` is only supported for index workspace writes.",
            )
        params: dict[str, Any] = {
            "artifact_path": artifact_path,
            "encoding": str(request.payload.get("encoding") or "utf-8"),
        }
        if mode == WORKSPACE_WRITE_MODE_APPEND:
            params["mode"] = mode
        if content_base64 is not None:
            params["content_base64"] = content_base64
        else:
            params["content"] = content or ""
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name=operation_name,
            message_type=RunnerMessageType.RUNTIME_WORKSPACE_WRITE,
            params=params,
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name=operation_name,
            expected_message_type=RunnerMessageType.RUNTIME_WORKSPACE_WRITE,
        )

    async def query_runtime_artifacts(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        operation_name = "query_runtime_artifacts"
        prefix = str(request.payload.get("prefix") or request.payload.get("path") or "")
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name=operation_name,
            message_type=RunnerMessageType.RUNTIME_WORKSPACE_QUERY,
            params={"prefix": prefix},
        )
        if not _should_wait_for_operation_result(request):
            return result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=result,
            operation_name=operation_name,
            expected_message_type=RunnerMessageType.RUNTIME_WORKSPACE_QUERY,
        )

    async def promote_artifact_refs(
        self,
        request: RuntimeOperationRequest,
    ) -> RuntimeOperationResult:
        """Declare canonical artifact refs on the runner data plane (upload only)."""
        operation_name = "promote_artifact_refs"
        tool_command_runtime_job_id = _resolve_optional_text(
            request.payload.get("tool_command_runtime_job_id")
            or request.metadata.get("tool_command_runtime_job_id")
        )
        if tool_command_runtime_job_id is None:
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name=operation_name,
                message="`tool_command_runtime_job_id` is required for promote_artifact_refs.",
            )
        params = dict(request.payload)
        dispatch_result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name=operation_name,
            message_type=RunnerMessageType.RUNTIME_ARTIFACT_PROMOTE,
            params=params,
        )
        if not _should_wait_for_operation_result(request):
            return dispatch_result
        return await self._operation_waiter._wait_for_runtime_operation_result(
            request=request,
            dispatch_result=dispatch_result,
            operation_name=operation_name,
            expected_message_type=RunnerMessageType.RUNTIME_ARTIFACT_PROMOTE,
        )
