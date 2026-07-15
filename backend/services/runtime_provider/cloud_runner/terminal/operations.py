"""Terminal operation handling for the cloud runner provider.

This module owns public terminal operation bodies for the cloud runner
provider. It delegates dispatch, terminal result waiting, stream attachment,
and frame-buffer access through bounded collaborators and does not import the
provider facade, artifact operations, or tool-command operations.
"""

from __future__ import annotations

from backend.services.runner_control.terminal_frame_buffer import get_runner_terminal_frame_buffer
from backend.services.runtime_provider.terminal_stream_contract import terminal_stream_from_payload
from runtime_shared.runner_protocol import RunnerMessageType

from ..dispatch.remote_dispatcher import CloudRunnerRemoteDispatcher
from ..error_codes import _RUNNER_ASSIGNMENT_REQUIRED, _RUNNER_TERMINAL_STREAM_UNAVAILABLE
from ..normalization import (
    _coerce_non_negative_float,
    _coerce_positive_int,
    _resolve_optional_text,
)
from ..payload_codec import _prepare_transport_params
from ..result_builders import CloudRunnerResultBuilder
from .result_waiter import CloudRunnerTerminalResultWaiter, _should_wait_for_terminal_result
from .stream_client import CloudRunnerTerminalStreamAttacher
from ...contracts import (
    RuntimeOperationRequest,
    RuntimeOperationResult,
    RuntimeOperationStatus,
    build_runtime_result,
)


class CloudRunnerTerminalOperations:
    """Handles cloud-runner terminal operation provider behavior."""

    def __init__(
        self,
        *,
        remote_dispatcher: CloudRunnerRemoteDispatcher,
        result_builder: CloudRunnerResultBuilder,
        terminal_streams: CloudRunnerTerminalStreamAttacher,
        terminal_result_waiter: CloudRunnerTerminalResultWaiter,
        provider_name: str,
    ) -> None:
        self._remote_dispatcher = remote_dispatcher
        self._result_builder = result_builder
        self._terminal_streams = terminal_streams
        self._terminal_result_waiter = terminal_result_waiter
        self._provider_name = provider_name

    async def open_terminal_session(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        if request.runner_id is None:
            return build_runtime_result(
                request,
                accepted=False,
                provider=self._provider_name,
                status=RuntimeOperationStatus.REJECTED,
                error_code=_RUNNER_ASSIGNMENT_REQUIRED,
                error_message="Runner terminal creation requires an assigned runner_id.",
                metadata={
                    "protocol_domain": "remote_runtime",
                    "operation_name": "open_terminal_session",
                },
            )
        params = dict(_prepare_transport_params(request.payload))
        params.setdefault("session_name", "runtime")
        params["cols"] = _coerce_positive_int(params.get("cols"), default=80)
        params["rows"] = _coerce_positive_int(params.get("rows"), default=24)
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="open_terminal_session",
            message_type=RunnerMessageType.TERMINAL_OPEN,
            params=params,
        )
        if not _should_wait_for_terminal_result(request):
            return result
        open_result = await self._terminal_result_waiter._wait_for_terminal_result(
            request=request,
            dispatch_result=result,
            operation_name="open_terminal_session",
            expected_terminal_operation="open",
        )
        return self._terminal_streams._attach_terminal_stream_client(request=request, result=open_result)

    async def send_terminal_input(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        stream_client = terminal_stream_from_payload(request.payload)
        if stream_client is None:
            params = dict(_prepare_transport_params(request.payload))
            session_id = _resolve_optional_text(params.get("session_id"))
            if session_id is None:
                return self._result_builder._invalid_request_result(
                    request=request,
                    operation_name="send_terminal_input",
                    message="`session_id` is required for send_terminal_input.",
                )
            params["session_id"] = session_id
            result = self._remote_dispatcher._dispatch_remote_operation(
                request=request,
                operation_name="send_terminal_input",
                message_type=RunnerMessageType.TERMINAL_INPUT,
                params=params,
            )
            if not _should_wait_for_terminal_result(request):
                return result
            return await self._terminal_result_waiter._wait_for_terminal_result(
                request=request,
                dispatch_result=result,
                operation_name="send_terminal_input",
                expected_terminal_operation="input",
            )
        if not self._terminal_streams._stream_client_channel_connected(stream_client):
            return self._terminal_stream_required_result(
                request=request,
                operation_name="send_terminal_input",
                message="Terminal input requires a connected runner terminal stream.",
            )
        data = request.payload.get("data", "")
        await stream_client.send_input(data if isinstance(data, (str, bytes)) else str(data))
        return build_runtime_result(
            request,
            accepted=True,
            provider=self._provider_name,
            status=RuntimeOperationStatus.SUCCEEDED,
            metadata={
                "protocol_domain": "remote_runtime",
                "operation_name": "send_terminal_input",
                "stream_mode": True,
            },
        )

    async def read_terminal_output(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        stream_client = terminal_stream_from_payload(request.payload)
        if stream_client is None:
            session_id = _resolve_optional_text(request.payload.get("session_id"))
            if session_id is None:
                return self._result_builder._invalid_request_result(
                    request=request,
                    operation_name="read_terminal_output",
                    message="`session_id` is required for read_terminal_output.",
                )
            runtime_job_id = _resolve_optional_text(request.payload.get("runtime_job_id"))
            cursor = _coerce_int_or_none(request.payload.get("cursor"))
            if cursor is None:
                cursor = _coerce_int_or_none(request.payload.get("after_sequence"))
            frame_result = get_runner_terminal_frame_buffer().read_frames(
                tenant_id=request.tenant_id,
                task_id=request.task_id,
                session_id=session_id,
                runtime_job_id=runtime_job_id,
                after_sequence=cursor,
                max_bytes=_coerce_positive_int(
                    request.payload.get("size")
                    if "size" in request.payload
                    else request.payload.get("max_bytes"),
                    default=4096,
                ),
            )
            delegate_result = dict(frame_result)
            delegate_result["success"] = True
            delegate_result["next_cursor"] = frame_result.get("next_sequence")
            return build_runtime_result(
                request=request,
                accepted=True,
                provider=self._provider_name,
                status=RuntimeOperationStatus.SUCCEEDED,
                metadata={
                    "protocol_domain": "remote_runtime",
                    "operation_name": "read_terminal_output",
                    "delegate_result": delegate_result,
                },
            )
        if not self._terminal_streams._stream_client_channel_connected(stream_client):
            return self._terminal_stream_required_result(
                request=request,
                operation_name="read_terminal_output",
                message="Terminal output reads require a connected runner terminal stream.",
            )
        size = _coerce_positive_int(
            request.payload.get("size")
            if "size" in request.payload
            else request.payload.get("max_bytes"),
            default=4096,
        )
        timeout = request.payload.get("timeout")
        timeout_value = None if timeout is None else _coerce_non_negative_float(timeout, default=0.0)
        data = await stream_client.read_output(size=size, timeout=timeout_value)
        return build_runtime_result(
            request,
            accepted=True,
            provider=self._provider_name,
            status=RuntimeOperationStatus.SUCCEEDED,
            metadata={
                "protocol_domain": "remote_runtime",
                "operation_name": "read_terminal_output",
                "stream_mode": True,
                "delegate_result": {
                    "session_id": stream_client.session_id,
                    "data": data,
                    "success": True,
                },
            },
        )

    async def resize_terminal_session(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        stream_client = terminal_stream_from_payload(request.payload)
        if stream_client is None:
            params = dict(_prepare_transport_params(request.payload))
            session_id = _resolve_optional_text(params.get("session_id"))
            if session_id is None:
                return self._result_builder._invalid_request_result(
                    request=request,
                    operation_name="resize_terminal_session",
                    message="`session_id` is required for resize_terminal_session.",
                )
            params["session_id"] = session_id
            params["cols"] = _coerce_positive_int(params.get("cols"), default=80)
            params["rows"] = _coerce_positive_int(params.get("rows"), default=24)
            result = self._remote_dispatcher._dispatch_remote_operation(
                request=request,
                operation_name="resize_terminal_session",
                message_type=RunnerMessageType.TERMINAL_RESIZE,
                params=params,
            )
            if not _should_wait_for_terminal_result(request):
                return result
            return await self._terminal_result_waiter._wait_for_terminal_result(
                request=request,
                dispatch_result=result,
                operation_name="resize_terminal_session",
                expected_terminal_operation="resize",
            )
        if not self._terminal_streams._stream_client_channel_connected(stream_client):
            return self._terminal_stream_required_result(
                request=request,
                operation_name="resize_terminal_session",
                message="Terminal resize requires a connected runner terminal stream.",
            )
        await stream_client.resize(
            _coerce_positive_int(request.payload.get("cols"), default=80),
            _coerce_positive_int(request.payload.get("rows"), default=24),
        )
        return build_runtime_result(
            request=request,
            accepted=True,
            provider=self._provider_name,
            status=RuntimeOperationStatus.SUCCEEDED,
            metadata={
                "protocol_domain": "remote_runtime",
                "operation_name": "resize_terminal_session",
                "stream_mode": True,
            },
        )

    async def close_terminal_session(self, request: RuntimeOperationRequest) -> RuntimeOperationResult:
        stream_client = terminal_stream_from_payload(request.payload)
        if stream_client is not None:
            await stream_client.close()

        params = dict(_prepare_transport_params(request.payload))
        session_id = _resolve_optional_text(params.get("session_id"))
        if session_id is None:
            if stream_client is not None:
                return build_runtime_result(
                    request,
                    accepted=True,
                    provider=self._provider_name,
                    status=RuntimeOperationStatus.SUCCEEDED,
                    metadata={
                        "protocol_domain": "remote_runtime",
                        "operation_name": "close_terminal_session",
                        "stream_mode": True,
                    },
                )
            return self._result_builder._invalid_request_result(
                request=request,
                operation_name="close_terminal_session",
                message="`session_id` is required for close_terminal_session.",
            )
        params["session_id"] = session_id
        result = self._remote_dispatcher._dispatch_remote_operation(
            request=request,
            operation_name="close_terminal_session",
            message_type=RunnerMessageType.TERMINAL_CLOSE,
            params=params,
        )
        if not _should_wait_for_terminal_result(request):
            return result
        return await self._terminal_result_waiter._wait_for_terminal_result(
            request=request,
            dispatch_result=result,
            operation_name="close_terminal_session",
            expected_terminal_operation="close",
        )

    def _terminal_stream_required_result(
        self,
        *,
        request: RuntimeOperationRequest,
        operation_name: str,
        message: str,
    ) -> RuntimeOperationResult:
        return build_runtime_result(
            request,
            accepted=False,
            provider=self._provider_name,
            status=RuntimeOperationStatus.REJECTED,
            error_code=_RUNNER_TERMINAL_STREAM_UNAVAILABLE,
            error_message=message,
            metadata={"protocol_domain": "remote_runtime", "operation_name": operation_name},
        )


def _coerce_int_or_none(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
